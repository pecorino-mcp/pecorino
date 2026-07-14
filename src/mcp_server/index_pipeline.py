import hashlib
import json
import logging
import os
import pathlib
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.core.constants import SUPPORTED_EXTENSIONS, get_language_for_extension
from src.mcp_server.index_db import CodeSearchIndex, find_repo_root, get_db_path_for_repo
from src.mcp_server.ramdisk import RamdiskIndex, RamdiskQuotaExceeded
from src.parsers.ast import ClassDef, InterfaceDef, walk
from src.parsers.tree_sitter_parser import parse_with_tree_sitter

logger = logging.getLogger(__name__)

class CodebaseIndexer:
    def __init__(self, repo_path: str = None):
        repo_path = repo_path if repo_path else find_repo_root(os.getcwd())

        from src.mcp_server.index_db import CodeSearchIndex
        from src.mcp_server.config import settings

        self.repo_path = repo_path
        db_path = get_db_path_for_repo(repo_path)
        self.search_index = CodeSearchIndex(db_path=db_path)
        self.graph = self.search_index._ensure_graph()
        
        self.enable_embeddings = settings.enable_embeddings
        if self.enable_embeddings:
            from src.mcp_server.embedding import EmbeddingPipeline
            self.embedder = EmbeddingPipeline()
        else:
            self.embedder = None
        self.enable_lsp = settings.enable_lsp
        self._repo_cache_lock = threading.Lock()

    def close(self):
        """Release the underlying DuckDB connection."""
        if self.search_index is not None:
            self.search_index.close()
            self.search_index = None
            self.graph = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _build_relationships_text(self, node) -> str:
        rels = []
        if getattr(node, 'called_methods', None):
            rels.append(f"CALLS: {', '.join(node.called_methods)}")
        accesses = []
        if getattr(node, 'read_attributes', None):
            accesses.append(f"READS: {', '.join(node.read_attributes)}")
        if getattr(node, 'mutated_attributes', None):
            accesses.append(f"MUTATES: {', '.join(node.mutated_attributes)}")
        if getattr(node, 'tainted_attributes', None):
            accesses.append(f"TAINTS: {', '.join(node.tainted_attributes)}")
        if accesses:
            rels.append(" ".join(accesses))

        extends_list = []
        if getattr(node, 'bases', None):
            extends_list.extend(node.bases)
        if getattr(node, 'extends', None):
            if isinstance(node.extends, list):
                extends_list.extend(node.extends)
            elif isinstance(node.extends, str):
                extends_list.append(node.extends)
        if extends_list:
            rels.append(f"EXTENDS: {', '.join(extends_list)}")

        if getattr(node, 'interfaces', None):
            rels.append(f"IMPLEMENTS: {', '.join(node.interfaces)}")

        return " ".join(rels)

    def _add_access(self, kind, attrs, parent_id, class_name, graph_nodes_dict, graph_edges, make_id):
        flags = {"read": (True, False, False), "mutate": (False, True, False), "taint": (False, False, True)}[kind]
        for attr in attrs:
            var_id = make_id("Variable", class_name, attr)
            graph_nodes_dict[var_id] = (var_id, {"name": f"{class_name}.{attr}"}, "Variable")
            graph_edges.append((parent_id, var_id, {"is_read": flags[0], "is_mutation": flags[1], "is_taint": flags[2]}, "ACCESSES_STATE"))

    def _add_state_accesses(self, node, parent_id, class_name, graph_nodes_dict, graph_edges, make_id):
        self._add_access("read", getattr(node, 'read_attributes', set()), parent_id, class_name, graph_nodes_dict, graph_edges, make_id)
        self._add_access("mutate", getattr(node, 'mutated_attributes', set()), parent_id, class_name, graph_nodes_dict, graph_edges, make_id)
        self._add_access("taint", getattr(node, 'tainted_attributes', set()), parent_id, class_name, graph_nodes_dict, graph_edges, make_id)

    def _add_lambdas(self, node, parent_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id):
        for lmd in getattr(node, 'lambdas', []):
            lmd_id = make_id("Lambda", parent_id, lmd.name, getattr(lmd, 'lineno', 0))
            graph_nodes_dict[lmd_id] = (lmd_id, {"name": lmd.name}, "Lambda")
            graph_edges.append((parent_id, lmd_id, {}, "CONTAINS_LAMBDA"))

            for call in getattr(lmd, 'called_methods', set()):
                symbol_id = f"Symbol::{call}"
                graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                graph_edges.append((lmd_id, symbol_id, {}, "CALLS"))

            self._add_state_accesses(lmd, lmd_id, class_name, graph_nodes_dict, graph_edges, make_id)
            self._add_lambdas(lmd, lmd_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id)

    def _add_statement_to_graph(self, statement, parent_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id):
        stmt_id = make_id("ControlFlow", statement.name, statement.lineno, statement.col_offset)
        graph_nodes_dict[stmt_id] = (stmt_id, {"name": statement.name, "type": statement.type}, "ControlFlow")
        graph_edges.append((parent_id, stmt_id, {}, "CONTAINS"))

        for call in getattr(statement, 'called_methods', set()):
            symbol_id = f"Symbol::{call}"
            graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
            graph_edges.append((stmt_id, symbol_id, {}, "CALLS"))

        self._add_state_accesses(statement, stmt_id, class_name, graph_nodes_dict, graph_edges, make_id)
        self._add_lambdas(statement, stmt_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id)

        for child_stmt in getattr(statement, 'statements', []):
            self._add_statement_to_graph(child_stmt, stmt_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id)

    def _find_file_in_repo(self, dep_string: str) -> str:
        """Find a file in the repo that matches dep_string (e.g. ending with it)."""
        with self._repo_cache_lock:
            if not hasattr(self, '_repo_files_cache'):
                self._repo_files_cache = []
                for r, d, fnames in os.walk(self.repo_path):
                    ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules"}
                    d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
                    for fname in fnames:
                        self._repo_files_cache.append(os.path.abspath(os.path.join(r, fname)))

        norm_dep = dep_string.replace('\\', '/').lstrip('/')
        for filepath in self._repo_files_cache:
            if filepath.replace('\\', '/').endswith('/' + norm_dep) or filepath.replace('\\', '/').endswith('/' + dep_string):
                return filepath
            if os.path.basename(filepath) == dep_string:
                return filepath
        return ""

    def _resolve_relative_fallback(self, dep_string: str, source_filepath: str) -> str:
        """Shared fallback: resolve a relative dep_string against the source file's directory."""
        return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))

    def _resolve_dependency(self, dep_string: str, source_filepath: str, file_extension: str) -> str:
        """Enhanced language-specific dependency file resolution."""
        # 1. JS/TS: Relative paths, node_modules, package.json resolution
        if file_extension in ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']:
            exts = ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']
            if dep_string.startswith('.'):
                base_dir = os.path.dirname(source_filepath)
                target_base = os.path.join(base_dir, dep_string)
                for ext in exts:
                    if os.path.exists(target_base + ext):
                        return os.path.abspath(target_base + ext)
                if os.path.isdir(target_base):
                    for ext in exts:
                        idx_path = os.path.join(target_base, f'index{ext}')
                        if os.path.exists(idx_path):
                            return os.path.abspath(idx_path)
            else:
                curr_dir = os.path.dirname(source_filepath)
                while True:
                    nm_path = os.path.join(curr_dir, 'node_modules', dep_string)
                    if os.path.exists(nm_path):
                        if os.path.isfile(nm_path):
                            return os.path.abspath(nm_path)
                        if os.path.isdir(nm_path):
                            pkg_json = os.path.join(nm_path, 'package.json')
                            if os.path.exists(pkg_json):
                                try:
                                    with open(pkg_json, encoding='utf-8') as f:
                                        pkg_data = json.load(f)
                                        main_file = pkg_data.get('main')
                                        if main_file:
                                            cand = os.path.abspath(os.path.join(nm_path, main_file))
                                            if os.path.exists(cand): return cand
                                            for ext in exts:
                                                if os.path.exists(cand + ext): return cand + ext
                                        exports = pkg_data.get('exports')
                                        if exports:
                                            if isinstance(exports, str):
                                                cand = os.path.abspath(os.path.join(nm_path, exports))
                                                if os.path.exists(cand): return cand
                                            elif isinstance(exports, dict) and isinstance(exports.get('.'), str):
                                                cand = os.path.abspath(os.path.join(nm_path, exports['.']))
                                                if os.path.exists(cand): return cand
                                except Exception:
                                    pass
                            for ext in exts:
                                cand = os.path.join(nm_path, f'index{ext}')
                                if os.path.exists(cand): return os.path.abspath(cand)
                    if curr_dir == self.repo_path or curr_dir == os.path.dirname(curr_dir):
                        break
                    curr_dir = os.path.dirname(curr_dir)
            if dep_string.startswith('.'):
                return self._resolve_relative_fallback(dep_string, source_filepath)
            return dep_string

        # 2. C/C++: Include paths
        elif file_extension in ['.cpp', '.cc', '.cxx', '.h', '.hpp']:
            base_dir = os.path.dirname(source_filepath)
            local_test = os.path.abspath(os.path.join(base_dir, dep_string))
            if os.path.exists(local_test): return local_test
            repo_test = os.path.abspath(os.path.join(self.repo_path, dep_string))
            if os.path.exists(repo_test): return repo_test
            for inc in ['', 'include', 'src', 'src/include']:
                test_path = os.path.abspath(os.path.join(self.repo_path, inc, dep_string))
                if os.path.exists(test_path): return test_path
            found = self._find_file_in_repo(dep_string)
            if found: return found
            if dep_string.startswith('.'): return self._resolve_relative_fallback(dep_string, source_filepath)
            return dep_string

        # 3. Python: relative imports (count dots) or absolute
        elif file_extension in ['.py', '.pyi']:
            if dep_string.startswith('.'):
                dots = len(dep_string) - len(dep_string.lstrip('.'))
                dep_without_dots = dep_string[dots:]
                base_dir = os.path.dirname(source_filepath)
                for _ in range(dots - 1):
                    base_dir = os.path.dirname(base_dir)
                parts = dep_without_dots.split('.') if dep_without_dots else []
                target_path = os.path.join(base_dir, *parts)
            else:
                parts = dep_string.split('.')
                target_path = os.path.join(self.repo_path, *parts)

            if os.path.exists(target_path + '.py'):
                return os.path.abspath(target_path + '.py')
            if os.path.isdir(target_path):
                init_path = os.path.join(target_path, '__init__.py')
                if os.path.exists(init_path): return os.path.abspath(init_path)
            if dep_string.startswith('.'):
                return os.path.abspath(target_path)
            return dep_string

        # 4. Go
        elif file_extension == '.go':
            module_name = ""
            go_mod = os.path.join(self.repo_path, 'go.mod')
            if os.path.exists(go_mod):
                try:
                    with open(go_mod, encoding='utf-8') as f:
                        for line in f:
                            if line.strip().startswith('module '):
                                module_name = line.strip().split()[1]
                                break
                except Exception:
                    pass
            if module_name and dep_string.startswith(module_name):
                relative_path = dep_string[len(module_name):].lstrip('/')
                target_path = os.path.abspath(os.path.join(self.repo_path, relative_path))
                if os.path.exists(target_path): return target_path
                if os.path.exists(target_path + '.go'): return target_path + '.go'

            target_path = os.path.abspath(os.path.join(self.repo_path, dep_string))
            if os.path.exists(target_path): return target_path
            found = self._find_file_in_repo(dep_string)
            if found: return found
            if dep_string.startswith('.'):
                return self._resolve_relative_fallback(dep_string, source_filepath)
            return dep_string

        # 5. Rust
        elif file_extension == '.rs':
            parts = dep_string.split('::')
            target_path = os.path.join(self.repo_path, *parts)
            if os.path.exists(target_path + '.rs'): return os.path.abspath(target_path + '.rs')
            if os.path.isdir(target_path):
                for mod_file in ['mod.rs', 'lib.rs', 'main.rs']:
                    mod_path = os.path.join(target_path, mod_file)
                    if os.path.exists(mod_path): return os.path.abspath(mod_path)
            found = self._find_file_in_repo(parts[-1] + '.rs')
            if found: return found
            if dep_string.startswith('.'):
                return self._resolve_relative_fallback(dep_string, source_filepath)
            return dep_string

        # 6. Fallback
        else:
            if dep_string.startswith('.'):
                return self._resolve_relative_fallback(dep_string, source_filepath)
            parts = dep_string.split('.')
            dep_suffix = "/".join(parts)
            for ext in ['.py', '.java', '.js', '.ts', '.go', '.rs', '.swift']:
                test_abs = os.path.join(self.repo_path, dep_suffix + ext)
                if os.path.exists(test_abs): return os.path.abspath(test_abs)
            return dep_string

    def _extract_records(self, content: str, filepath: str, file_extension: str) -> dict:
        if file_extension == ".md" and "docs/adr" in filepath.replace("\\", "/"):
            # Simple markdown parsing for ADRs
            first_line = content.splitlines()[0] if content else "Untitled"
            title = first_line.lstrip('# ') if first_line.startswith('#') else "Untitled"
            nodes_to_index = [{
                "id": filepath,
                "name": title,
                "filepath": filepath,
                "signature": "",
                "body_text": content,
                "start_line": 1,
                "end_line": len(content.splitlines()) or 1,
                "node_type": "ADR",
                "metrics": {}
            }]
            graph_nodes_dict = {
                filepath: (filepath, {
                    "name": title,
                    "path": filepath,
                    "extension": ".md",
                    "lang": "markdown"
                }, "ADR")
            }
            return {
                "nodes_to_index": nodes_to_index,
                "graph_nodes": graph_nodes_dict,
                "graph_edges": [],
                "dependencies": []
            }
            
        tree = parse_with_tree_sitter(content, file_extension)
        if not tree:
            return None

        nodes_to_index = []
        graph_nodes_dict = {}
        graph_edges = []
        dependencies = set()

        # Determine if file is a test file
        is_test_file = "/tests/" in filepath or "/test_" in filepath or "test_" in os.path.basename(filepath) or "_test" in os.path.basename(filepath)
        file_label = "TestFile" if is_test_file else "File"
        file_id = filepath

        lang_name = get_language_for_extension(file_extension)
        graph_nodes_dict[file_id] = (file_id, {
            "name": os.path.basename(filepath),
            "path": filepath,
            "extension": file_extension,
            "lang": lang_name
        }, file_label)

        def make_id(*parts):
            return "::".join([filepath] + [str(p) for p in parts])

        def process_methods(methods, class_id, class_name):
            for m in methods:
                m_cc = getattr(m, 'cyclomatic_complexity', 1)
                m_cog = getattr(m, 'cognitive_complexity', 0)
                m_rec = getattr(m, 'is_recursive', False)
                m_is_test = getattr(m, 'is_test', False) or is_test_file
                
                method_metrics = {
                    'cyclomatic_complexity': m_cc,
                    'cognitive_complexity': m_cog,
                    'is_recursive': m_rec,
                    'is_test': m_is_test,
                    'raised_exceptions': list(getattr(m, 'raised_exceptions', []))
                }
                
                m_args = getattr(m, 'args', [])
                sig = f"{class_name}.{m.name}({', '.join(m_args)})" if m_args else f"{class_name}.{m.name}()"
                nodes_to_index.append({
                    'name': f"{class_name}.{m.name}",
                    'node_type': 'method',
                    'filepath': filepath,
                    'start_line': m.lineno,
                    'end_line': m.end_lineno,
                    'start_byte': getattr(m, 'start_byte', 0),
                    'end_byte': getattr(m, 'end_byte', 0),
                    'metrics': method_metrics,
                    'relationships': self._build_relationships_text(m),
                    'complexity': m_cc,
                    'signature': sig,
                })
                method_id = make_id(class_name, m.name)
                graph_nodes_dict[method_id] = (method_id, {
                    "name": m.name,
                    "complexity": m_cc,
                    "filepath": filepath,
                    "start_line": m.lineno,
                    "end_line": m.end_lineno,
                }, "Method")
                graph_edges.append((class_id, method_id, {}, "CONTAINS"))

                for call in getattr(m, 'called_methods', set()):
                    symbol_id = f"Symbol::{call}"
                    graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                    graph_edges.append((method_id, symbol_id, {}, "CALLS"))

                for exc in getattr(m, 'raised_exceptions', set()):
                    symbol_id = f"Symbol::{exc}"
                    graph_nodes_dict[symbol_id] = (symbol_id, {"name": exc}, "Symbol")
                    graph_edges.append((method_id, symbol_id, {}, "RAISES"))

                self._add_state_accesses(m, method_id, class_name, graph_nodes_dict, graph_edges, make_id)
                self._add_lambdas(m, method_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id)

                for stmt in getattr(m, 'statements', []):
                    self._add_statement_to_graph(stmt, method_id, filepath, class_name, graph_nodes_dict, graph_edges, make_id)

        for node in walk(tree):
            if isinstance(node, ClassDef):
                wmc = getattr(node, 'wmc', 0)
                metrics = {'wmc': wmc, 'cbo': getattr(node, 'cbo', 0), 'rfc': getattr(node, 'rfc', 0), 'lcom': getattr(node, 'lcom', 0)}
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'class',
                    'filepath': filepath,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'start_byte': getattr(node, 'start_byte', 0),
                    'end_byte': getattr(node, 'end_byte', 0),
                    'metrics': metrics,
                    'relationships': self._build_relationships_text(node)
                })

                class_id = make_id(node.name)
                graph_nodes_dict[class_id] = (class_id, {
                    "name": node.name,
                    "filepath": filepath,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }, "Class")
                graph_edges.append((file_id, class_id, {}, "CONTAINS"))

                for base in getattr(node, 'bases', []):
                    symbol_id = f"Symbol::{base}"
                    graph_nodes_dict[symbol_id] = (symbol_id, {"name": base}, "Symbol")
                    graph_edges.append((class_id, symbol_id, {}, "EXTENDS"))

                for interface in getattr(node, 'interfaces', []):
                    symbol_id = f"Symbol::{interface}"
                    graph_nodes_dict[symbol_id] = (symbol_id, {"name": interface}, "Symbol")
                    graph_edges.append((class_id, symbol_id, {}, "IMPLEMENTS"))

                process_methods(node.methods, class_id, node.name)

            elif isinstance(node, InterfaceDef):
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'interface',
                    'filepath': filepath,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'start_byte': getattr(node, 'start_byte', 0),
                    'end_byte': getattr(node, 'end_byte', 0),
                    'metrics': {},
                    'relationships': self._build_relationships_text(node)
                })
                class_id = make_id(node.name)
                graph_nodes_dict[class_id] = (class_id, {
                    "name": node.name,
                    "filepath": filepath,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }, "Interface")
                graph_edges.append((file_id, class_id, {}, "CONTAINS"))

                process_methods(node.methods, class_id, node.name)

            elif type(node).__name__ == 'ImportDef':
                if node.module:
                    dependencies.add(node.module)

        for func in tree.functions:
            f_cc = getattr(func, 'cyclomatic_complexity', 1)
            f_cog = getattr(func, 'cognitive_complexity', 0)
            f_rec = getattr(func, 'is_recursive', False)
            f_is_test = getattr(func, 'is_test', False) or is_test_file
            
            func_metrics = {
                'cyclomatic_complexity': f_cc,
                'cognitive_complexity': f_cog,
                'is_recursive': f_rec,
                'is_test': f_is_test,
                'raised_exceptions': list(getattr(func, 'raised_exceptions', []))
            }
            f_args = getattr(func, 'args', [])
            sig = f"{func.name}({', '.join(f_args)})" if f_args else f"{func.name}()"
            nodes_to_index.append({
                'name': func.name,
                'node_type': 'function',
                'filepath': filepath,
                'start_line': func.lineno,
                'end_line': func.end_lineno,
                'start_byte': getattr(func, 'start_byte', 0),
                'end_byte': getattr(func, 'end_byte', 0),
                'metrics': func_metrics,
                'relationships': self._build_relationships_text(func),
                'complexity': f_cc,
                'signature': sig,
            })
            func_id = make_id(func.name)
            graph_nodes_dict[func_id] = (func_id, {
                "name": func.name,
                "complexity": f_cc,
                "filepath": filepath,
                "start_line": func.lineno,
                "end_line": func.end_lineno,
            }, "Function")
            graph_edges.append((file_id, func_id, {}, "CONTAINS"))

            for call in getattr(func, 'called_methods', set()):
                symbol_id = f"Symbol::{call}"
                graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                graph_edges.append((func_id, symbol_id, {}, "CALLS"))

            for exc in getattr(func, 'raised_exceptions', set()):
                symbol_id = f"Symbol::{exc}"
                graph_nodes_dict[symbol_id] = (symbol_id, {"name": exc}, "Symbol")
                graph_edges.append((func_id, symbol_id, {}, "RAISES"))

            self._add_state_accesses(func, func_id, "Global", graph_nodes_dict, graph_edges, make_id)
            self._add_lambdas(func, func_id, filepath, "Global", graph_nodes_dict, graph_edges, make_id)

            for stmt in getattr(func, 'statements', []):
                self._add_statement_to_graph(stmt, func_id, filepath, "Global", graph_nodes_dict, graph_edges, make_id)

        # Process HTTP Routes
        for r in getattr(tree, 'routes', []):
            route_id = make_id("Route", r.http_method, r.path)
            graph_nodes_dict[route_id] = (route_id, {
                "name": r.name,
                "http_method": r.http_method,
                "path": r.path
            }, "Route")
            graph_edges.append((file_id, route_id, {}, "CONTAINS"))

            nodes_to_index.append({
                'name': r.name,
                'node_type': 'route',
                'filepath': filepath,
                'start_line': r.lineno,
                'end_line': r.end_lineno,
                'start_byte': getattr(r, 'start_byte', 0),
                'end_byte': getattr(r, 'end_byte', 0),
                'metrics': {},
                'relationships': ""
            })

            # Map CONTAINS from parent Function/Method
            parent_id = None
            for func in tree.functions:
                if func.start_byte <= r.start_byte and r.end_byte <= func.end_byte:
                    parent_id = make_id(func.name)
                    break
            if not parent_id:
                for cls in tree.classes:
                    for m in cls.methods:
                        if m.start_byte <= r.start_byte and r.end_byte <= m.end_byte:
                            parent_id = make_id(cls.name, m.name)
                            break
                    if parent_id:
                        break
            if parent_id:
                graph_edges.append((parent_id, route_id, {}, "CONTAINS"))

        # Process Environment Variables
        for ev in getattr(tree, 'env_vars', []):
            ev_id = f"EnvVar::{ev.name}"
            graph_nodes_dict[ev_id] = (ev_id, {
                "name": ev.name
            }, "EnvVar")
            graph_edges.append((file_id, ev_id, {}, "CONTAINS"))

            nodes_to_index.append({
                'name': ev.name,
                'node_type': 'env_var',
                'filepath': filepath,
                'start_line': 1,
                'end_line': 1,
                'start_byte': 0,
                'end_byte': 0,
                'metrics': {},
                'relationships': ""
            })

        resolved_deps = []
        for dep in dependencies:
            resolved_dep = self._resolve_dependency(dep, filepath, file_extension)
            resolved_deps.append((dep, resolved_dep))

        return {
            "nodes_to_index": nodes_to_index,
            "graph_nodes": list(graph_nodes_dict.values()),
            "graph_edges": graph_edges,
            "resolved_deps": resolved_deps
        }

    def index_file(self, filepath: str, content: str, file_extension: str, rebuild_fts: bool = False):
        """Parse and index a single file for search and dependency graph."""
        MAX_FILE_SIZE = 2 * 1024 * 1024
        if len(content.encode('utf-8', errors='ignore')) > MAX_FILE_SIZE:
            logger.warning("Skipping %s (exceeds 2MB limit)", filepath)
            return

        records = self._extract_records(content, filepath, file_extension)
        if not records:
            return

        self._embed_nodes(records.get("nodes_to_index", []), content.encode('utf-8'))

        self.search_index.clear_file(filepath)

        nodes_to_index = records["nodes_to_index"]
        graph_nodes_dict = {nid: (nid, props, lbl) for nid, props, lbl in records["graph_nodes"]}
        graph_edges = records["graph_edges"]
        file_id = filepath

        for dep, resolved_dep in records["resolved_deps"]:
            if os.path.exists(resolved_dep) and os.path.isabs(resolved_dep):
                graph_nodes_dict[resolved_dep] = (resolved_dep, {"name": os.path.basename(resolved_dep), "path": resolved_dep}, "File")
                graph_edges.append((file_id, resolved_dep, {}, "DEPENDS_ON"))
            else:
                graph_nodes_dict[resolved_dep] = (resolved_dep, {"name": resolved_dep}, "Module")
                graph_edges.append((file_id, resolved_dep, {}, "DEPENDS_ON"))

        if nodes_to_index:
            self.search_index.index_nodes(nodes_to_index)

        graph_nodes = list(graph_nodes_dict.values())
        if graph_nodes:
            try:
                id_map = self.graph.insert_nodes_bulk(graph_nodes)
                if graph_edges:
                    self.graph.insert_edges_bulk(graph_edges, id_map)
            except Exception as e:
                logger.warning("Failed to insert graph nodes/edges for %s: %s", filepath, e)
                logger.debug(traceback.format_exc())

        if rebuild_fts:
            try:
                self.search_index.rebuild_fts()
            except Exception as e:
                logger.warning("Failed to rebuild FTS index for %s: %s", filepath, e)
                logger.debug(traceback.format_exc())
        else:
            self.search_index.mark_fts_dirty()


    def _parse_file_task(self, fp: Any, file_str: str, mtime: float):
        try:
            MAX_FILE_SIZE = 2 * 1024 * 1024
            if fp.stat().st_size > MAX_FILE_SIZE:
                return None

            content = fp.read_text(encoding='utf-8', errors='ignore')
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            if getattr(self, "lsp_client", None):
                try:
                    self.lsp_client.open_document(file_str, content)
                except Exception as e:
                    logger.debug("Failed to open document in LSP: %s", e)

            records = self._extract_records(content, file_str, fp.suffix)
            if not records:
                return None
                
            content_bytes = content.encode('utf-8')

            lsp_resolutions = []
            if getattr(self, "lsp_client", None):
                try:
                    from src.parsers.tree_sitter_parser import get_raw_tree_sitter_tree
                    raw_tree = get_raw_tree_sitter_tree(content, fp.suffix)
                    if raw_tree:
                        lsp_resolutions = self._find_lsp_resolutions(raw_tree, file_str)
                except Exception as e:
                    logger.debug("Failed to resolve definitions via LSP: %s", e)

            records.update({
                "file_str": file_str,
                "content_hash": content_hash,
                "mtime": mtime,
                "lang": fp.suffix,
                "content_bytes": content_bytes,
                "lsp_resolutions": lsp_resolutions
            })
            return records
        except Exception as e:
            logger.warning("Failed to parse %s: %s", file_str, e)
            logger.debug(traceback.format_exc())
            return None

    def _embed_nodes(self, nodes_to_index: list, content_bytes: bytes):
        if not self.enable_embeddings or not self.embedder:
            return
        if not nodes_to_index:
            return
            
        texts_to_embed = []
        for n in nodes_to_index:
            s_byte = n.get('start_byte', 0)
            e_byte = n.get('end_byte', 0)
            if e_byte > s_byte:
                try:
                    text = content_bytes[s_byte:e_byte].decode('utf-8', errors='ignore')
                except Exception:
                    text = ""
            else:
                text = ""
            texts_to_embed.append(text)
            
        embeddings = self.embedder.embed_batch(texts_to_embed)
        for i, n in enumerate(nodes_to_index):
            if i < len(embeddings):
                n['embedding'] = embeddings[i]

    def _compute_git_coupling(self, dirpath: str) -> list:
        """Run git log to calculate file co-change coupling scores (Jaccard similarity).
        
        Returns list of (filepath_a, filepath_b, weight) tuples.
        """
        import subprocess
        from collections import defaultdict
        
        try:
            cmd = ["git", "log", "--pretty=format:commit:%H", "--name-only"]
            proc = subprocess.Popen(
                cmd,
                cwd=dirpath,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            stdout, _ = proc.communicate()
            if proc.returncode != 0 or not stdout:
                return []
        except Exception as e:
            logger.debug("Failed to run git log: %s", e)
            return []
            
        commit_files = defaultdict(set)
        current_commit = None
        
        file_commit_counts = defaultdict(int)
        
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("commit:"):
                current_commit = line.split(":", 1)[1]
            elif current_commit:
                abs_path = os.path.abspath(os.path.join(dirpath, line))
                if os.path.exists(abs_path):
                    commit_files[current_commit].add(abs_path)
                    
        pair_counts = defaultdict(lambda: defaultdict(int))
        for commit, files in commit_files.items():
            if len(files) > 50 or len(files) < 2:
                continue
            for f in files:
                file_commit_counts[f] += 1
                
            file_list = list(files)
            for i in range(len(file_list)):
                for j in range(i + 1, len(file_list)):
                    f1, f2 = file_list[i], file_list[j]
                    if f1 < f2:
                        pair_counts[f1][f2] += 1
                    else:
                        pair_counts[f2][f1] += 1
                        
        coupling_edges = []
        for f1, targets in pair_counts.items():
            for f2, count in targets.items():
                denom = file_commit_counts[f1] + file_commit_counts[f2] - count
                if denom > 0:
                    jaccard = count / denom
                    if jaccard >= 0.1 and count >= 2:
                        coupling_edges.append((f1, f2, float(jaccard)))
                        
        return coupling_edges

    def _find_lsp_resolutions(self, tree, file_str) -> list:
        if not getattr(self, "lsp_client", None):
            return []
            
        resolutions = []
        
        def visit(n):
            if n.type in ('call_expression', 'method_invocation', 'call'):
                func_node = n.child_by_field_name('function') or n.child_by_field_name('name')
                if not func_node and n.children:
                    func_node = n.children[0]
                if func_node:
                    line = func_node.start_point[0] + 1
                    char = func_node.start_point[1]
                    try:
                        res = self.lsp_client.resolve_definition(file_str, line, char)
                        if res:
                            resolutions.append({
                                "call_line": line,
                                "def_filepath": res["filepath"],
                                "def_line": res["start_line"]
                            })
                    except Exception:
                        pass
            for child in n.children:
                visit(child)
                
        visit(tree.root_node)
        return resolutions
    def _compute_similarity_edges(self):
        """Compute SIMILAR_TO (MinHash/Jaccard) and SEMANTICALLY_RELATED (Vector) edges."""
        logger.info("Computing similarity edges (SIMILAR_TO, SEMANTICALLY_RELATED)...")
        try:
            # 1. SEMANTICALLY_RELATED (Vector Similarity)
            if self.enable_embeddings and self.search_index:
                # Use DuckDB cross join for nodes in different files with cosine distance <= 0.20 (score >= 0.80)
                semantic_query = """
                SELECT a.id as from_id, b.id as to_id, 1.0 - array_cosine_distance(a.embedding, b.embedding) as score
                FROM code_nodes a, code_nodes b
                WHERE a.id < b.id
                  AND a.node_type IN ('Function', 'Method', 'Class')
                  AND b.node_type IN ('Function', 'Method', 'Class')
                  AND a.filepath != b.filepath
                  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                  AND array_cosine_distance(a.embedding, b.embedding) <= 0.20
                """
                df_semantic = self.search_index._conn.execute(semantic_query).df()
                if not df_semantic.empty:
                    semantic_edges = []
                    for _, row in df_semantic.iterrows():
                        semantic_edges.extend([
                            (row['from_id'], row['to_id'], float(row['score'])),
                            (row['to_id'], row['from_id'], float(row['score']))
                        ])
                    if semantic_edges:
                        # Write to temporary CSV and load into Kùzu
                        import tempfile
                        import csv
                        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as f:
                            writer = csv.writer(f)
                            for edge in semantic_edges:
                                writer.writerow(edge)
                            tmp_path = f.name
                        
                        try:
                            with self.graph:
                                self.graph._conn.execute(f"COPY SEMANTICALLY_RELATED FROM '{tmp_path}' (HEADER=false);")
                            logger.info(f"Loaded {len(semantic_edges)} SEMANTICALLY_RELATED edges.")
                        finally:
                            os.remove(tmp_path)

            # 2. SIMILAR_TO (MinHash/Jaccard via datasketch)
            try:
                from datasketch import MinHash, MinHashLSH
                # Fetch text content for code nodes
                text_query = "SELECT id, content FROM code_nodes WHERE node_type IN ('Function', 'Method', 'Class')"
                df_texts = self.search_index._conn.execute(text_query).df()
                
                if not df_texts.empty:
                    lsh = MinHashLSH(threshold=0.70, num_perm=128)
                    minhashes = {}
                    
                    for _, row in df_texts.iterrows():
                        nid = row['id']
                        content = str(row['content'])
                        # Basic 3-gram tokenization
                        tokens = [content[i:i+3] for i in range(len(content) - 2)]
                        m = MinHash(num_perm=128)
                        for d in tokens:
                            m.update(d.encode('utf8'))
                        lsh.insert(nid, m)
                        minhashes[nid] = m
                    
                    similar_edges = []
                    seen_pairs = set()
                    for nid, m in minhashes.items():
                        result = lsh.query(m)
                        for r_id in result:
                            if nid != r_id and (r_id, nid) not in seen_pairs:
                                jaccard = m.jaccard(minhashes[r_id])
                                if jaccard >= 0.70:
                                    similar_edges.extend([
                                        (nid, r_id, float(jaccard)),
                                        (r_id, nid, float(jaccard))
                                    ])
                                    seen_pairs.add((nid, r_id))
                                    
                    if similar_edges:
                        import tempfile
                        import csv
                        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as f:
                            writer = csv.writer(f)
                            for edge in similar_edges:
                                writer.writerow(edge)
                            tmp_path = f.name
                            
                        try:
                            with self.graph:
                                self.graph._conn.execute(f"COPY SIMILAR_TO FROM '{tmp_path}' (HEADER=false);")
                            logger.info(f"Loaded {len(similar_edges)} SIMILAR_TO edges.")
                        finally:
                            os.remove(tmp_path)
                            
            except ImportError:
                logger.warning("datasketch not installed. Skipping SIMILAR_TO edge generation.")
                
        except Exception as e:
            logger.warning(f"Failed to compute similarity edges: {e}")
            logger.debug(traceback.format_exc())


    def _post_process_graph(self):
        """Find recursive self-calls and resolve Symbol nodes to Method/Function for dynamic languages."""
        self._compute_similarity_edges()
        
        queries = [
            "MATCH (m:CodeNode {node_type: 'Method'})-[r:RECURSES_TO]->(m) DELETE r",
            "MATCH (f:CodeNode {node_type: 'Function'})-[r:RECURSES_TO]->(f) DELETE r",
            "MATCH (m:CodeNode {node_type: 'Method'})-[:CALLS]->(m) CREATE (m)-[:RECURSES_TO]->(m)",
            "MATCH (f:CodeNode {node_type: 'Function'})-[:CALLS]->(f) CREATE (f)-[:RECURSES_TO]->(f)",
            # Resolve CALLS to Symbol nodes into direct CALLS to Method/Function nodes.
            # Handle dotted attribute access: 'self.rebuild_fts' → match Method named 'rebuild_fts'
            # Handle class-qualified: 'search_index.rebuild_fts' → match 'rebuild_fts'
            # Method callers → Method targets
            "MATCH (caller:CodeNode {node_type: 'Method'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (m:CodeNode {node_type: 'Method'}) WHERE s.name = m.name OR ends_with(s.name, '.' + m.name) CREATE (caller)-[:CALLS]->(m)",
            # Method callers → Function targets
            "MATCH (caller:CodeNode {node_type: 'Method'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (f:CodeNode {node_type: 'Function'}) WHERE s.name = f.name OR ends_with(s.name, '.' + f.name) CREATE (caller)-[:CALLS]->(f)",
            # Function callers → Method targets
            "MATCH (caller:CodeNode {node_type: 'Function'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (m:CodeNode {node_type: 'Method'}) WHERE s.name = m.name OR ends_with(s.name, '.' + m.name) CREATE (caller)-[:CALLS]->(m)",
            # Function callers → Function targets
            "MATCH (caller:CodeNode {node_type: 'Function'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (f:CodeNode {node_type: 'Function'}) WHERE s.name = f.name OR ends_with(s.name, '.' + f.name) CREATE (caller)-[:CALLS]->(f)",
            # ControlFlow callers → Method/Function targets
            "MATCH (caller:CodeNode {node_type: 'ControlFlow'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (m:CodeNode {node_type: 'Method'}) WHERE s.name = m.name OR ends_with(s.name, '.' + m.name) CREATE (caller)-[:CALLS]->(m)",
            "MATCH (caller:CodeNode {node_type: 'ControlFlow'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (f:CodeNode {node_type: 'Function'}) WHERE s.name = f.name OR ends_with(s.name, '.' + f.name) CREATE (caller)-[:CALLS]->(f)",
            # Lambda callers → Method/Function targets
            "MATCH (caller:CodeNode {node_type: 'Lambda'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (m:CodeNode {node_type: 'Method'}) WHERE s.name = m.name OR ends_with(s.name, '.' + m.name) CREATE (caller)-[:CALLS]->(m)",
            "MATCH (caller:CodeNode {node_type: 'Lambda'})-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (f:CodeNode {node_type: 'Function'}) WHERE s.name = f.name OR ends_with(s.name, '.' + f.name) CREATE (caller)-[:CALLS]->(f)",
        ]
        try:
            with self.graph:
                self.graph.query_batch(queries)

            # Log resolution stats
            try:
                with self.graph:
                    total_calls = self.graph.query("MATCH ()-[r:CALLS]->() RETURN count(r) AS cnt")
                    to_symbol = self.graph.query("MATCH ()-[:CALLS]->(s:CodeNode {node_type: 'Symbol'}) RETURN count(s) AS cnt")
                    to_resolved = self.graph.query("MATCH ()-[:CALLS]->(t:CodeNode) WHERE t.node_type <> 'Symbol' RETURN count(t) AS cnt")
                    total = total_calls[0].get('cnt', 0) if total_calls else 0
                    sym = to_symbol[0].get('cnt', 0) if to_symbol else 0
                    res = to_resolved[0].get('cnt', 0) if to_resolved else 0
                    logger.info("Symbol resolution: %d total CALLS, %d resolved, %d still pointing to Symbol nodes", total, res, sym)
            except Exception:
                pass

            # After graph relationships are resolved, calculate and build PageRank
            try:
                pr_scores = self.graph.pagerank()
                if pr_scores:
                    self.search_index.update_pagerank_bulk(pr_scores)
            except Exception as e:
                logger.warning("Failed to calculate or build PageRank: %s", e)

            # Calculate and build Leiden Communities
            try:
                communities = self.graph.compute_leiden_communities()
                if communities:
                    partitions = [{'node_id': k, 'community_id': v} for k, v in communities.items()]
                    self.search_index.update_community_bulk(partitions)
            except Exception as e:
                logger.warning("Failed to calculate or build Leiden communities: %s", e)

            # Compute in/out degree from CALLS edges
            try:
                with self.graph:
                    out_rows = self.graph.query(
                        "MATCH (n)-[:CALLS]->(t:CodeNode) WHERE t.node_type <> 'Symbol' "
                        "RETURN n.name AS name, count(t) AS deg"
                    )
                    in_rows = self.graph.query(
                        "MATCH (s:CodeNode)-[:CALLS]->(n) WHERE s.node_type <> 'Symbol' "
                        "RETURN n.name AS name, count(s) AS deg"
                    )
                degree_map = {}
                for row in out_rows:
                    name = row.get("name", "")
                    degree_map.setdefault(name, {"name": name, "in_degree": 0, "out_degree": 0})
                    degree_map[name]["out_degree"] = row.get("deg", 0)
                for row in in_rows:
                    name = row.get("name", "")
                    degree_map.setdefault(name, {"name": name, "in_degree": 0, "out_degree": 0})
                    degree_map[name]["in_degree"] = row.get("deg", 0)
                if degree_map:
                    self.search_index.update_degrees_bulk(list(degree_map.values()))
            except Exception as e:
                logger.warning("Failed to compute in/out degree: %s", e)

            # Leiden Sweep for community detection
            try:
                from src.mcp_server import graph_algorithms
                logger.info("Starting Leiden sweep for community detection...")
                
                # Project graph first
                with self.graph:
                    try:
                        self.graph._conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                    except Exception:
                        pass
                    self.graph._conn.execute("""
                        CALL PROJECT_GRAPH('CodeGraph', 
                            ['File', 'Class', 'Method', 'Function', 'Interface', 'Symbol', 'Module', 'ControlFlow', 'Lambda', 'Variable', 'Folder', 'TestFile', 'Route', 'EnvVar'],
                            ['DEPENDS_ON', 'CONTAINS', 'EXTENDS', 'IMPLEMENTS', 'CALLS', 'FILE_CHANGES_WITH', 'RAISES', 'TESTS', 'HTTP_CALLS']
                        );
                    """)
                
                    sweep_results = graph_algorithms.sweep_gamma(self.graph, graph_name='CodeGraph')
                    stable_regions = graph_algorithms.find_stable_partition(sweep_results)
                    best_partition_info = graph_algorithms.get_best_partition(stable_regions)
                    
                    if best_partition_info:
                        partition_dict = best_partition_info["partition"]
                        community_updates = [{"node_id": k, "community_id": v} for k, v in partition_dict.items()]
                        self.search_index.update_community_bulk(community_updates)
                        logger.info("Successfully updated community IDs based on best partition (gamma=%.2f)", best_partition_info["gamma_begin"])
                    else:
                        logger.warning("No stable partition found from Leiden sweep.")

                    # Drop projected graph
                    try:
                        self.graph._conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Failed to run Leiden sweep: %s", e)
                logger.debug(traceback.format_exc())

        except Exception as e:
            logger.warning("Failed to post-process graph: %s", e)
            logger.debug(traceback.format_exc())

    def index_directory(self, dirpath: str, progress_callback=None) -> dict:
        path = pathlib.Path(dirpath).resolve()
        self.lsp_client = None
        if self.enable_lsp:
            try:
                from src.mcp_server.lsp.manager import LSPClient
                self.lsp_client = LSPClient(workspace_root=str(path))
                if not self.lsp_client.start():
                    self.lsp_client = None
            except Exception as e:
                logger.warning("Failed to start LSP client: %s", e)
                self.lsp_client = None
        try:
            return self._index_directory_impl(dirpath, progress_callback)
        finally:
            if self.lsp_client:
                self.lsp_client.stop()
                self.lsp_client = None

    def _index_directory_impl(self, dirpath: str, progress_callback=None) -> dict:
        path = pathlib.Path(dirpath).resolve()
        ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules", "third_party", "dataset", "build_test", "build-context"}
        files = []
        for r, d, fnames in os.walk(str(path)):
            d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
            for fname in fnames:
                fp = (pathlib.Path(r) / fname).resolve()
                if fp.suffix in SUPPORTED_EXTENSIONS:
                    files.append(fp)

        total_files = len(files)
        try:
            total_source_bytes = sum(fp.stat().st_size for fp in files)
        except Exception:
            total_source_bytes = 0

        projected_db_bytes = int(total_source_bytes * 40.0)
        required_ramdisk_bytes = max(int(projected_db_bytes * 1.5), 100 * 1024 * 1024)

        if progress_callback:
            progress_callback(0, total_files, f"Projected raw DB size: {projected_db_bytes / (1024*1024):.1f} MB")

        # Get existing metadata to avoid concurrent DB queries in threads
        tracked_metadata = {}
        try:
            if self.search_index and self.search_index._conn:
                rows = self.search_index._conn.execute('SELECT filepath, content_hash, mtime FROM files').fetchall()
                tracked_metadata = {row[0]: (row[1], row[2]) for row in rows}
        except Exception:
            pass

        indexed_count = 0
        skipped_count = 0
        current_files_set = set()
        parse_jobs = []

        for fp in files:
            file_str = str(fp)
            current_files_set.add(file_str)
            try:
                mtime = os.path.getmtime(file_str)
                if file_str in tracked_metadata:
                    existing_hash, existing_mtime = tracked_metadata[file_str]
                    if abs(mtime - existing_mtime) < 0.01:
                        skipped_count += 1
                        continue
                parse_jobs.append((fp, file_str, mtime))
            except Exception as e:
                logger.warning("Failed to stat %s: %s", file_str, e)

        results = []
        if parse_jobs:
            max_workers = min(8, os.cpu_count() or 4)
            current_processed = skipped_count

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                job_iterator = iter(parse_jobs)
                futures = {}

                # Pre-populate pool (backpressure queue size = 2 * max_workers)
                for _ in range(2 * max_workers):
                    try:
                        job = next(job_iterator)
                        fut = executor.submit(self._parse_file_task, job[0], job[1], job[2])
                        futures[fut] = job[1]
                    except StopIteration:
                        break

                while futures:
                    done = next(as_completed(futures))
                    file_str = futures.pop(done)
                    try:
                        res = done.result()
                        if res:
                            # Verify hash in main thread before keeping
                            existing_hash = tracked_metadata.get(file_str, (None, None))[0]
                            if existing_hash == res["content_hash"]:
                                skipped_count += 1
                            else:
                                results.append(res)
                    except Exception as e:
                        logger.warning("Task failed for %s: %s", file_str, e)

                    current_processed += 1
                    if progress_callback:
                        progress_callback(current_processed, total_files, f"Parsed {file_str}")

                    try:
                        job = next(job_iterator)
                        fut = executor.submit(self._parse_file_task, job[0], job[1], job[2])
                        futures[fut] = job[1]
                    except StopIteration:
                        pass

        # Batch embed all nodes across all parsed files in one go
        if results and self.enable_embeddings and self.embedder:
            all_nodes_to_embed = []
            texts_to_embed = []
            for res in results:
                content_bytes = res.get("content_bytes", b"")
                for n in res.get("nodes_to_index", []):
                    s_byte = n.get('start_byte', 0)
                    e_byte = n.get('end_byte', 0)
                    if e_byte > s_byte:
                        try:
                            text = content_bytes[s_byte:e_byte].decode('utf-8', errors='ignore')
                        except Exception:
                            text = ""
                    else:
                        text = ""
                    texts_to_embed.append(text)
                    all_nodes_to_embed.append(n)

            if texts_to_embed:
                if progress_callback:
                    progress_callback(total_files, total_files, f"Generating vector embeddings for {len(texts_to_embed)} code symbols...")
                try:
                    embeddings = self.embedder.embed_batch(texts_to_embed)
                    for i, n in enumerate(all_nodes_to_embed):
                        if i < len(embeddings):
                            n['embedding'] = embeddings[i]
                except Exception as e:
                    logger.warning("Failed to generate vector embeddings during bulk run: %s", e)
                    logger.debug(traceback.format_exc())

        # Clear content_bytes to save memory
        if results:
            for res in results:
                res.pop("content_bytes", None)

        # Close existing connections before mass update/ramdisk to prevent connection errors
        ssd_db_path = self.search_index.db_path
        self.close()

        if not results:
            self.search_index = CodeSearchIndex(ssd_db_path)
            self.graph = self.search_index.graph

            tracked_files = self.search_index.get_all_tracked_files()
            stale_files = [tf for tf in tracked_files if tf not in current_files_set]
            stale_count = len(stale_files)
            if stale_files:
                if progress_callback:
                    progress_callback(total_files, total_files, f"Removing {stale_count} stale files...")
                with self.graph:
                    self.search_index.clear_files_bulk(stale_files)

            fts_error = None
            if not self.search_index.has_fts_index() or self.search_index.is_fts_dirty():
                try:
                    self.search_index.rebuild_fts()
                except Exception as e:
                    fts_error = str(e)
                    logger.warning("Failed to rebuild FTS index: %s", fts_error)

            res = {
                "status": "success" if not fts_error else "partial",
                "indexed_files": 0,
                "skipped_files": skipped_count,
                "stale_files_removed": stale_count,
                "total_files_found": total_files
            }
            if fts_error:
                res["fts_error"] = fts_error
            return res

        class DummyContext:
            def __init__(self, db_path):
                self.db_path = db_path
            def __enter__(self): return self
            def __exit__(self, exc_type, exc_val, exc_tb): return False
            def check_quota(self): pass
            def get_usage_bytes(self): return 0

        # Ramdisk Fallback Check
        use_ramdisk = True
        try:
            if os.path.exists('/dev/shm') and os.path.isdir('/dev/shm'):
                shm_free = shutil.disk_usage('/dev/shm').free
                if required_ramdisk_bytes > (shm_free - 50 * 1024 * 1024):
                    use_ramdisk = False
                    logger.info("[ramdisk] Not enough /dev/shm (Free: %.1fMB, Req: %.1fMB), using SSD directly", shm_free/(1024*1024), required_ramdisk_bytes/(1024*1024))
            else:
                use_ramdisk = False
        except Exception:
            use_ramdisk = False

        ramdisk = RamdiskIndex(ssd_db_path, max_bytes=required_ramdisk_bytes) if use_ramdisk else DummyContext(ssd_db_path)

        with ramdisk:
            ram_search = CodeSearchIndex(ramdisk.db_path)
            ram_graph = ram_search.graph

            with ram_graph:
                files_to_clear = [res["file_str"] for res in results]
                if files_to_clear:
                    if progress_callback:
                        progress_callback(total_files, total_files, "Clearing existing indexes for modified files...")
                    ram_search.clear_files_bulk(files_to_clear)

                all_search_nodes = []
                all_graph_nodes = {}
                all_graph_edges = set()
                files_metadata = []

                for res in results:
                    file_str = res["file_str"]
                    if res["nodes_to_index"]:
                        all_search_nodes.extend(res["nodes_to_index"])

                    if res["graph_nodes"]:
                        for node_id, props, lbl in res["graph_nodes"]:
                            all_graph_nodes[node_id] = (props, lbl)

                    if res["graph_edges"]:
                        for src, dst, props, rel in res["graph_edges"]:
                            all_graph_edges.add((src, dst, frozenset(props.items()), rel))

                    for dep, resolved_dep in res["resolved_deps"]:
                        if os.path.exists(resolved_dep) and os.path.isabs(resolved_dep):
                            dep_name = os.path.basename(resolved_dep)
                            dep_ext = os.path.splitext(resolved_dep)[1]
                            if resolved_dep not in all_graph_nodes:
                                all_graph_nodes[resolved_dep] = ({"name": dep_name, "path": resolved_dep, "extension": dep_ext}, "File")
                            all_graph_edges.add((file_str, resolved_dep, frozenset(), "DEPENDS_ON"))
                        else:
                            if resolved_dep not in all_graph_nodes:
                                all_graph_nodes[resolved_dep] = ({"name": resolved_dep}, "Module")
                            all_graph_edges.add((file_str, resolved_dep, frozenset(), "DEPENDS_ON"))

                    lang_name = get_language_for_extension(res["lang"])
                    files_metadata.append((file_str, res["content_hash"], res["mtime"], lang_name))

                try:
                    if all_search_nodes:
                        if progress_callback:
                            progress_callback(total_files, total_files, "Saving search nodes to RAM DuckDB...")
                        ram_search.index_nodes(all_search_nodes)

                    if all_graph_nodes:
                        if progress_callback:
                            progress_callback(total_files, total_files, "Inserting graph nodes into RAM Gorgonzola...")
                        nodes_list = [(nid, props, lbl) for nid, (props, lbl) in all_graph_nodes.items()]
                        id_map = ram_graph.insert_nodes_bulk(nodes_list)

                        # Resolve LSP definitions into concrete CALLS edges
                        for res in results:
                            file_str = res["file_str"]
                            for resolution in res.get("lsp_resolutions", []):
                                call_line = resolution["call_line"]
                                def_filepath = resolution["def_filepath"]
                                def_line = resolution["def_line"]
                                
                                caller_id = None
                                for node_id, (props, label) in all_graph_nodes.items():
                                    if node_id.startswith(file_str + "::"):
                                        if label in ("Method", "Function"):
                                            if props.get("start_line", 0) <= call_line <= props.get("end_line", 0):
                                                caller_id = node_id
                                                break
                                                
                                callee_id = None
                                for node_id, (props, label) in all_graph_nodes.items():
                                    if node_id.startswith(def_filepath + "::") or node_id == def_filepath:
                                        if label in ("Method", "Function", "Class"):
                                            if props.get("start_line", 0) <= def_line <= props.get("end_line", 0):
                                                callee_id = node_id
                                                break
                                                
                                if not callee_id and self.search_index:
                                    try:
                                        row = self.search_index._conn.execute(
                                            "SELECT id FROM code_nodes WHERE filepath = ? AND start_line <= ? AND end_line >= ? AND node_type IN ('method', 'function', 'class')",
                                            (def_filepath, def_line, def_line)
                                        ).fetchone()
                                        if row:
                                            callee_id = row[0]
                                    except Exception:
                                        pass
                                        
                                if caller_id and callee_id:
                                    all_graph_edges.add((caller_id, callee_id, frozenset(), "CALLS"))

                        if all_graph_edges:
                            if progress_callback:
                                progress_callback(total_files, total_files, "Linking graph edges in RAM...")
                            edges_list = [(src, dst, dict(props), rel) for src, dst, props, rel in all_graph_edges]
                            ram_graph.insert_edges_bulk(edges_list, id_map)

                        # Compute and insert git temporal coupling edges
                        git_coupling = self._compute_git_coupling(str(path))
                        if git_coupling:
                            if progress_callback:
                                progress_callback(total_files, total_files, f"Linking {len(git_coupling)} git temporal coupling edges...")
                            git_edges = []
                            for f1, f2, weight in git_coupling:
                                lbl1 = ram_graph._get_node_label(f1, ram_graph._conn)
                                lbl2 = ram_graph._get_node_label(f2, ram_graph._conn)
                                if lbl1 and lbl2:
                                    git_edges.append((f1, f2, {"weight": weight}, "FILE_CHANGES_WITH"))
                            if git_edges:
                                ram_graph.insert_edges_bulk(git_edges, id_map)

                    ramdisk.check_quota()
                    if files_metadata:
                        if progress_callback:
                            progress_callback(total_files, total_files, "Updating file hash tracking in RAM...")
                        ram_search.upsert_file_hashes_bulk(files_metadata)

                    indexed_count = len(results)
                except RamdiskQuotaExceeded as e:
                    logger.error("[ramdisk] QUOTA EXCEEDED: %s", e)
                    raise
                except Exception as e:
                    logger.warning("Failed during bulk database write: %s", e)
                    logger.debug(traceback.format_exc())

            ram_search.close()

        self.search_index = CodeSearchIndex(ssd_db_path)
        self.graph = self.search_index.graph

        tracked_files = self.search_index.get_all_tracked_files()
        stale_files = [tf for tf in tracked_files if tf not in current_files_set]
        stale_count = len(stale_files)
        if stale_files:
            if progress_callback:
                progress_callback(total_files, total_files, f"Removing {stale_count} stale files...")
            with self.graph:
                self.search_index.clear_files_bulk(stale_files)

        self._post_process_graph()

        fts_error = None
        try:
            if progress_callback:
                progress_callback(total_files, total_files, "Rebuilding FTS index...")
            self.search_index.rebuild_fts()
        except Exception as e:
            fts_error = str(e)
            logger.warning("Failed to rebuild FTS index: %s", fts_error)
            logger.debug(traceback.format_exc())

        if progress_callback:
            progress_callback(total_files, total_files, "Resolving symbols...")

        from src.mcp_server.graph_api import GraphAPI
        graph_api = GraphAPI(dirpath)
        graph_api.resolve_symbols()

        self.search_index.close()

        res = {
            "status": "success" if not fts_error else "partial",
            "indexed_files": indexed_count,
            "skipped_files": skipped_count,
            "stale_files_removed": stale_count,
            "total_files_found": total_files
        }
        if fts_error:
            res["fts_error"] = fts_error
        return res

def progress_callback(current: int, total: int, file_path: str):
    print(json.dumps({
        "current": current,
        "total": total,
        "file": file_path
    }), flush=True)

def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: python -m src.mcp_server.index_pipeline <repo_root> <target_path>\n")
        sys.exit(1)

    repo_root = sys.argv[1]
    target_path = sys.argv[2]

    try:
        indexer = CodebaseIndexer(repo_path=repo_root)
        res = indexer.index_directory(target_path, progress_callback=progress_callback)
        print(json.dumps({"result": res}), flush=True)
    except Exception as e:
        sys.stderr.write(f"Error during indexing subprocess: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
