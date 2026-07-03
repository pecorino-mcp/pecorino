import os
import sys
import json
import traceback
import hashlib
import shutil
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Set, Tuple

from src.mcp_server.index_db import CodeSearchIndex, get_db_path_for_repo, find_repo_root
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
from src.mcp_server.ramdisk import RamdiskIndex, RamdiskQuotaExceeded

from src.parsers.ast import ClassDef, InterfaceDef, walk
from src.parsers.tree_sitter_parser import parse_with_tree_sitter
from src.core.constants import get_language_for_extension, SUPPORTED_EXTENSIONS

class CodebaseIndexer:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        db_path = get_db_path_for_repo(self.repo_path)
        
        # Let CodeSearchIndex own the graph instance to avoid multiple connection handlers
        self.search_index = CodeSearchIndex(db_path)
        self.graph = self.search_index.graph
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
                    ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist"}
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
                                    with open(pkg_json, 'r', encoding='utf-8') as f:
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
                return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))
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
            if dep_string.startswith('.'): return local_test
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
                    with open(go_mod, 'r', encoding='utf-8') as f:
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
                return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))
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
                return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))
            return dep_string

        # 6. Fallback
        else:
            if dep_string.startswith('.'):
                return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))
            parts = dep_string.split('.')
            dep_suffix = "/".join(parts)
            for ext in ['.py', '.java', '.js', '.ts', '.go', '.rs', '.swift']:
                test_abs = os.path.join(self.repo_path, dep_suffix + ext)
                if os.path.exists(test_abs): return os.path.abspath(test_abs)
            return dep_string

    def _extract_records(self, content: str, filepath: str, file_extension: str) -> dict:
        tree = parse_with_tree_sitter(content, file_extension)
        if not tree:
            return None

        nodes_to_index = []
        graph_nodes_dict = {}
        graph_edges = []
        dependencies = set()

        file_id = filepath
        lang_name = get_language_for_extension(file_extension)
        graph_nodes_dict[file_id] = (file_id, {
            "name": os.path.basename(filepath),
            "path": filepath,
            "extension": file_extension,
            "lang": lang_name
        }, "File")

        def make_id(*parts):
            return "::".join([filepath] + [str(p) for p in parts])

        def process_methods(methods, class_id, class_name):
            for m in methods:
                m_cc = getattr(m, 'cyclomatic_complexity', 1)
                method_metrics = {'cyclomatic_complexity': m_cc}
                nodes_to_index.append({
                    'name': f"{class_name}.{m.name}",
                    'node_type': 'method',
                    'filepath': filepath,
                    'start_line': m.lineno,
                    'end_line': m.end_lineno,
                    'metrics': method_metrics,
                    'relationships': self._build_relationships_text(m)
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
            func_metrics = {'cyclomatic_complexity': f_cc}
            nodes_to_index.append({
                'name': func.name,
                'node_type': 'function',
                'filepath': filepath,
                'start_line': func.lineno,
                'end_line': func.end_lineno,
                'metrics': func_metrics,
                'relationships': self._build_relationships_text(func)
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
                
            self._add_state_accesses(func, func_id, "Global", graph_nodes_dict, graph_edges, make_id)
            self._add_lambdas(func, func_id, filepath, "Global", graph_nodes_dict, graph_edges, make_id)
                
            for stmt in getattr(func, 'statements', []):
                self._add_statement_to_graph(stmt, func_id, filepath, "Global", graph_nodes_dict, graph_edges, make_id)

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
            print(f"Warning: Skipping {filepath} (exceeds 2MB limit)", file=sys.stderr)
            return

        records = self._extract_records(content, filepath, file_extension)
        if not records:
            return
            
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
                print(f"Warning: Failed to insert graph nodes/edges for {filepath}: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

        if rebuild_fts:
            try:
                self.search_index.rebuild_fts()
            except Exception as e:
                print(f"Warning: Failed to rebuild FTS index for {filepath}: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
        else:
            self.search_index.mark_fts_dirty()


    def _parse_file_task(self, fp: Any, file_str: str, mtime: float):
        try:
            MAX_FILE_SIZE = 2 * 1024 * 1024
            if fp.stat().st_size > MAX_FILE_SIZE:
                return None
                
            content = fp.read_text(encoding='utf-8', errors='ignore')
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            
            records = self._extract_records(content, file_str, fp.suffix)
            if not records:
                return None
                
            records.update({
                "file_str": file_str,
                "content_hash": content_hash,
                "mtime": mtime,
                "lang": fp.suffix
            })
            return records
        except Exception as e:
            print(f"Warning: Failed to parse {file_str}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return None

    def _post_process_recursion(self):
        """Find recursive self-calls and add RECURSES_TO relationships."""
        queries = [
            "MATCH (m:Method)-[r:RECURSES_TO]->(m) DELETE r",
            "MATCH (f:Function)-[r:RECURSES_TO]->(f) DELETE r",
            "MATCH (m:Method)-[:CALLS]->(m) CREATE (m)-[:RECURSES_TO]->(m)",
            "MATCH (f:Function)-[:CALLS]->(f) CREATE (f)-[:RECURSES_TO]->(f)"
        ]
        try:
            with self.graph:
                self.graph.query_batch(queries)
        except Exception as e:
            print(f"Warning: Failed to post-process recursion: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    def index_directory(self, dirpath: str, progress_callback=None) -> dict:
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
        except Exception as e:
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
                print(f"Warning: Failed to stat {file_str}: {e}", file=sys.stderr)

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
                        print(f"Warning: Task failed for {file_str}: {e}", file=sys.stderr)
                        
                    current_processed += 1
                    if progress_callback:
                        progress_callback(current_processed, total_files, f"Parsed {file_str}")
                        
                    try:
                        job = next(job_iterator)
                        fut = executor.submit(self._parse_file_task, job[0], job[1], job[2])
                        futures[fut] = job[1]
                    except StopIteration:
                        pass

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
                    print(f"Warning: Failed to rebuild FTS index: {fts_error}", file=sys.stderr)

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
                    print(f"[ramdisk] Not enough /dev/shm (Free: {shm_free/(1024*1024):.1f}MB, Req: {required_ramdisk_bytes/(1024*1024):.1f}MB), using SSD directly", file=sys.stderr)
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
                        
                        if all_graph_edges:
                            if progress_callback:
                                progress_callback(total_files, total_files, "Linking graph edges in RAM...")
                            edges_list = [(src, dst, dict(props), rel) for src, dst, props, rel in all_graph_edges]
                            ram_graph.insert_edges_bulk(edges_list, id_map)

                    ramdisk.check_quota()
                    if files_metadata:
                        if progress_callback:
                            progress_callback(total_files, total_files, "Updating file hash tracking in RAM...")
                        ram_search.upsert_file_hashes_bulk(files_metadata)
                        
                    indexed_count = len(results)
                except RamdiskQuotaExceeded as e:
                    print(f"[ramdisk] QUOTA EXCEEDED: {e}", file=sys.stderr, flush=True)
                    raise
                except Exception as e:
                    print(f"Warning: Failed during bulk database write: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

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
                
        self._post_process_recursion()

        fts_error = None
        try:
            if progress_callback:
                progress_callback(total_files, total_files, "Rebuilding FTS index...")
            self.search_index.rebuild_fts()
        except Exception as e:
            fts_error = str(e)
            print(f"Warning: Failed to rebuild FTS index: {fts_error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

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
