import os
import sys
import json
import asyncio
from typing import Dict, Any, List

from src.mcp_server.index_db import CodeSearchIndex, get_db_path_for_repo, find_repo_root
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
from src.mcp_server.ramdisk import RamdiskIndex, RamdiskQuotaExceeded

from src.parsers.ast import ClassDef, InterfaceDef, walk
from src.parsers.tree_sitter_parser import parse_with_tree_sitter

class CodebaseIndexer:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        db_path = get_db_path_for_repo(self.repo_path)
        
        self.graph = GorgonzolaGraph(db_path=db_path)
        self.search_index = CodeSearchIndex(db_path)
        self.search_index.graph = self.graph

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
        if getattr(node, 'bases', None):
            rels.append(f"EXTENDS: {', '.join(node.bases)}")
        if getattr(node, 'interfaces', None):
            rels.append(f"IMPLEMENTS: {', '.join(node.interfaces)}")
        if getattr(node, 'extends', None):
            rels.append(f"EXTENDS: {', '.join(node.extends)}")
        return " ".join(rels)

    def _add_state_accesses(self, node, parent_id, class_name, graph_nodes_dict, graph_edges):
        for attr in getattr(node, 'read_attributes', set()):
            var_id = f"Variable::{class_name}.{attr}"
            graph_nodes_dict[var_id] = (var_id, {"name": f"{class_name}.{attr}"}, "Variable")
            graph_edges.append((parent_id, var_id, {"is_read": True, "is_mutation": False, "is_taint": False}, "ACCESSES_STATE"))

        for attr in getattr(node, 'mutated_attributes', set()):
            var_id = f"Variable::{class_name}.{attr}"
            graph_nodes_dict[var_id] = (var_id, {"name": f"{class_name}.{attr}"}, "Variable")
            graph_edges.append((parent_id, var_id, {"is_read": False, "is_mutation": True, "is_taint": False}, "ACCESSES_STATE"))

        for attr in getattr(node, 'tainted_attributes', set()):
            var_id = f"Variable::{class_name}.{attr}"
            graph_nodes_dict[var_id] = (var_id, {"name": f"{class_name}.{attr}"}, "Variable")
            graph_edges.append((parent_id, var_id, {"is_read": False, "is_mutation": False, "is_taint": True}, "ACCESSES_STATE"))

    def _add_lambdas(self, node, parent_id, filepath, class_name, graph_nodes_dict, graph_edges):
        for lmd in getattr(node, 'lambdas', []):
            lmd_id = f"Lambda::{lmd.name}"
            graph_nodes_dict[lmd_id] = (lmd_id, {"name": lmd.name}, "Lambda")
            graph_edges.append((parent_id, lmd_id, {}, "CONTAINS_LAMBDA"))
            
            for call in getattr(lmd, 'called_methods', set()):
                symbol_id = f"Symbol::{call}"
                graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                graph_edges.append((lmd_id, symbol_id, {}, "CALLS"))
                
            self._add_state_accesses(lmd, lmd_id, class_name, graph_nodes_dict, graph_edges)
            self._add_lambdas(lmd, lmd_id, filepath, class_name, graph_nodes_dict, graph_edges)

    def _add_statement_to_graph(self, statement, parent_id, filepath, class_name, graph_nodes_dict, graph_edges):
        stmt_id = f"{filepath}::{statement.name}::{statement.lineno}:{statement.col_offset}"
        graph_nodes_dict[stmt_id] = (stmt_id, {"name": statement.name, "type": statement.type}, "ControlFlow")
        graph_edges.append((parent_id, stmt_id, {}, "CONTAINS"))
        
        for call in getattr(statement, 'called_methods', set()):
            symbol_id = f"Symbol::{call}"
            graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
            graph_edges.append((stmt_id, symbol_id, {}, "CALLS"))
            
        self._add_state_accesses(statement, stmt_id, class_name, graph_nodes_dict, graph_edges)
        self._add_lambdas(statement, stmt_id, filepath, class_name, graph_nodes_dict, graph_edges)
            
        for child_stmt in getattr(statement, 'statements', []):
            self._add_statement_to_graph(child_stmt, stmt_id, filepath, class_name, graph_nodes_dict, graph_edges)

    def _resolve_dependency(self, dep_string: str, source_filepath: str, file_extension: str) -> str:
        """Enhanced language-specific dependency file resolution."""
        # 1. JS/TS: Relative paths or modules
        if file_extension in ['.js', '.jsx', '.ts', '.tsx']:
            if dep_string.startswith('.'):
                base_dir = os.path.dirname(source_filepath)
                target_base = os.path.join(base_dir, dep_string)
                for ext in ['.js', '.jsx', '.ts', '.tsx']:
                    if os.path.exists(target_base + ext):
                        return os.path.abspath(target_base + ext)
                # Check directory for index files
                if os.path.isdir(target_base):
                    for ext in ['index.js', 'index.ts', 'index.jsx', 'index.tsx']:
                        idx_path = os.path.join(target_base, ext)
                        if os.path.exists(idx_path):
                            return os.path.abspath(idx_path)
            return dep_string # Return as module name
            
        # 2. C/C++: Direct paths usually relative to repo root or current file
        elif file_extension in ['.cpp', '.cc', '.cxx', '.h', '.hpp']:
            # Try relative to current file
            base_dir = os.path.dirname(source_filepath)
            local_test = os.path.join(base_dir, dep_string)
            if os.path.exists(local_test):
                return os.path.abspath(local_test)
            # Try relative to repo root
            repo_test = os.path.join(self.repo_path, dep_string)
            if os.path.exists(repo_test):
                return os.path.abspath(repo_test)
            return dep_string

        # 3. Python: dot-separated, resolve to .py or __init__.py
        elif file_extension in ['.py', '.pyi']:
            if dep_string.startswith('.'):
                base_dir = os.path.dirname(source_filepath)
                # Python relative imports are a bit tricky, but let's approximate
                target_path = os.path.join(base_dir, dep_string.lstrip('.'))
            else:
                parts = dep_string.split('.')
                target_path = os.path.join(self.repo_path, *parts)
            
            if os.path.exists(target_path + '.py'):
                return os.path.abspath(target_path + '.py')
            if os.path.isdir(target_path):
                init_path = os.path.join(target_path, '__init__.py')
                if os.path.exists(init_path):
                    return os.path.abspath(init_path)
            return dep_string
            
        # 4. Go: slash-separated paths
        elif file_extension == '.go':
            target_path = os.path.join(self.repo_path, dep_string)
            if os.path.exists(target_path + '.go'):
                return os.path.abspath(target_path + '.go')
            if os.path.isdir(target_path):
                return os.path.abspath(target_path)
            return dep_string
            
        # 5. Rust: :: separated paths
        elif file_extension == '.rs':
            parts = dep_string.split('::')
            target_path = os.path.join(self.repo_path, *parts)
            if os.path.exists(target_path + '.rs'):
                return os.path.abspath(target_path + '.rs')
            if os.path.isdir(target_path):
                mod_path = os.path.join(target_path, 'mod.rs')
                if os.path.exists(mod_path):
                    return os.path.abspath(mod_path)
            return dep_string

        # 6. Fallback (Java, Swift, etc)
        else:
            parts = dep_string.split('.')
            dep_suffix = "/".join(parts)
            for ext in ['.py', '.java', '.js', '.ts', '.go', '.rs', '.swift']:
                test_rel_path = dep_suffix + ext
                test_abs_path = os.path.join(self.repo_path, test_rel_path)
                if os.path.exists(test_abs_path):
                    return os.path.abspath(test_abs_path)
            return dep_string

    def index_file(self, filepath: str, content: str, file_extension: str, rebuild_fts: bool = False):
        """Parse and index a single file for search and dependency graph."""
        tree = parse_with_tree_sitter(content, file_extension)
        if not tree:
            return
            
        self.search_index.clear_file(filepath)
        
        nodes_to_index = []
        
        graph_nodes_dict = {}
        graph_edges = []
        
        file_id = filepath
        graph_nodes_dict[file_id] = (file_id, {"name": os.path.basename(filepath), "path": filepath, "extension": file_extension}, "File")

        dependencies = set()

        for node in walk(tree):
            if isinstance(node, ClassDef):
                # Get metrics with defaults since we don't have CK metrics calculator
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
                
                class_id = f"{filepath}::{node.name}"
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
                for m in node.methods:
                    m_cc = getattr(m, 'cyclomatic_complexity', 1)
                    method_metrics = {'cyclomatic_complexity': m_cc}
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': method_metrics,
                        'relationships': self._build_relationships_text(m)
                    })
                    method_id = f"{class_id}::{m.name}"
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

                    self._add_state_accesses(m, method_id, node.name, graph_nodes_dict, graph_edges)
                    self._add_lambdas(m, method_id, filepath, node.name, graph_nodes_dict, graph_edges)

                    for stmt in getattr(m, 'statements', []):
                        self._add_statement_to_graph(stmt, method_id, filepath, node.name, graph_nodes_dict, graph_edges)

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
                class_id = f"{filepath}::{node.name}"
                graph_nodes_dict[class_id] = (class_id, {
                    "name": node.name,
                    "filepath": filepath,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }, "Interface")
                graph_edges.append((file_id, class_id, {}, "CONTAINS"))
                for m in node.methods:
                    m_cc = getattr(m, 'cyclomatic_complexity', 1)
                    method_metrics = {'cyclomatic_complexity': m_cc}
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': method_metrics,
                        'relationships': self._build_relationships_text(m)
                    })
                    method_id = f"{class_id}::{m.name}"
                    graph_nodes_dict[method_id] = (method_id, {
                        "name": m.name,
                        "complexity": m_cc,
                        "filepath": filepath,
                        "start_line": m.lineno,
                        "end_line": m.end_lineno,
                    }, "Method")
                    graph_edges.append((class_id, method_id, {}, "CONTAINS"))
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
            func_id = f"{filepath}::{func.name}"
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
                
            self._add_state_accesses(func, func_id, "Global", graph_nodes_dict, graph_edges)
            self._add_lambdas(func, func_id, filepath, "Global", graph_nodes_dict, graph_edges)
                
            for stmt in getattr(func, 'statements', []):
                self._add_statement_to_graph(stmt, func_id, filepath, "Global", graph_nodes_dict, graph_edges)

        # Add dependencies to graph
        for dep in dependencies:
            resolved_dep = self._resolve_dependency(dep, filepath, file_extension)
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
                import sys
                print(f"Warning: Failed to insert graph nodes/edges for {filepath}: {e}", file=sys.stderr)

        if rebuild_fts:
            try:
                self.search_index.rebuild_fts()
            except Exception as e:
                import sys
                print(f"Warning: Failed to rebuild FTS index for {filepath}: {e}", file=sys.stderr)


    def _parse_file_task(self, fp: Any, file_str: str, content_hash: str, mtime: float):
        try:
            content = fp.read_text(encoding='utf-8', errors='ignore')
            file_extension = fp.suffix
            tree = parse_with_tree_sitter(content, file_extension)
            if not tree:
                return None
                
            nodes_to_index = []
            graph_nodes_dict = {}
            graph_edges = []
            
            file_id = file_str
            from src.core.constants import get_language_for_extension
            lang_name = get_language_for_extension(file_extension)
            graph_nodes_dict[file_id] = (file_id, {
                "name": os.path.basename(file_str),
                "path": file_str,
                "extension": file_extension,
                "content_hash": content_hash,
                "mtime": float(mtime),
                "lang": lang_name
            }, "File")
            dependencies = set()

            for node in walk(tree):
                if isinstance(node, ClassDef):
                    wmc = getattr(node, 'wmc', 0)
                    metrics = {'wmc': wmc, 'cbo': getattr(node, 'cbo', 0), 'rfc': getattr(node, 'rfc', 0), 'lcom': getattr(node, 'lcom', 0)}
                    nodes_to_index.append({
                        'name': node.name,
                        'node_type': 'class',
                        'filepath': file_str,
                        'start_line': node.lineno,
                        'end_line': node.end_lineno,
                        'metrics': metrics
                    })
                    
                    class_id = f"{file_str}::{node.name}"
                    graph_nodes_dict[class_id] = (class_id, {
                        "name": node.name,
                        "filepath": file_str,
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
                    for m in node.methods:
                        m_cc = getattr(m, 'cyclomatic_complexity', 1)
                        method_metrics = {'cyclomatic_complexity': m_cc}
                        nodes_to_index.append({
                            'name': f"{node.name}.{m.name}",
                            'node_type': 'method',
                            'filepath': file_str,
                            'start_line': m.lineno,
                            'end_line': m.end_lineno,
                            'metrics': method_metrics
                        })
                        method_id = f"{class_id}::{m.name}"
                        graph_nodes_dict[method_id] = (method_id, {
                            "name": m.name,
                            "complexity": m_cc,
                            "filepath": file_str,
                            "start_line": m.lineno,
                            "end_line": m.end_lineno,
                        }, "Method")
                        graph_edges.append((class_id, method_id, {}, "CONTAINS"))
                        
                        for call in getattr(m, 'called_methods', set()):
                            symbol_id = f"Symbol::{call}"
                            graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                            graph_edges.append((method_id, symbol_id, {}, "CALLS"))
                elif isinstance(node, InterfaceDef):
                    nodes_to_index.append({
                        'name': node.name,
                        'node_type': 'interface',
                        'filepath': file_str,
                        'start_line': node.lineno,
                        'end_line': node.end_lineno,
                        'metrics': {}
                    })
                    class_id = f"{file_str}::{node.name}"
                    graph_nodes_dict[class_id] = (class_id, {
                        "name": node.name,
                        "filepath": file_str,
                        "start_line": node.lineno,
                        "end_line": node.end_lineno,
                    }, "Interface")
                    graph_edges.append((file_id, class_id, {}, "CONTAINS"))
                    for m in node.methods:
                        m_cc = getattr(m, 'cyclomatic_complexity', 1)
                        method_metrics = {'cyclomatic_complexity': m_cc}
                        nodes_to_index.append({
                            'name': f"{node.name}.{m.name}",
                            'node_type': 'method',
                            'filepath': file_str,
                            'start_line': m.lineno,
                            'end_line': m.end_lineno,
                            'metrics': method_metrics
                        })
                        method_id = f"{class_id}::{m.name}"
                        graph_nodes_dict[method_id] = (method_id, {
                            "name": m.name,
                            "complexity": m_cc,
                            "filepath": file_str,
                            "start_line": m.lineno,
                            "end_line": m.end_lineno,
                        }, "Method")
                        graph_edges.append((class_id, method_id, {}, "CONTAINS"))
                elif type(node).__name__ == 'ImportDef':
                    if node.module:
                        dependencies.add(node.module)

            for func in tree.functions:
                f_cc = getattr(func, 'cyclomatic_complexity', 1)
                func_metrics = {'func_cc': f_cc}
                nodes_to_index.append({
                    'name': func.name,
                    'node_type': 'function',
                    'filepath': file_str,
                    'start_line': func.lineno,
                    'end_line': func.end_lineno,
                    'metrics': func_metrics
                })
                func_id = f"{file_str}::{func.name}"
                graph_nodes_dict[func_id] = (func_id, {
                    "name": func.name,
                    "complexity": f_cc,
                    "filepath": file_str,
                    "start_line": func.lineno,
                    "end_line": func.end_lineno,
                }, "Function")
                graph_edges.append((file_id, func_id, {}, "CONTAINS"))
                
                for call in getattr(func, 'called_methods', set()):
                    symbol_id = f"Symbol::{call}"
                    graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                    graph_edges.append((func_id, symbol_id, {}, "CALLS"))

            resolved_deps = []
            for dep in dependencies:
                resolved_dep = self._resolve_dependency(dep, file_str, file_extension)
                resolved_deps.append((dep, resolved_dep))

            return {
                "file_str": file_str,
                "content_hash": content_hash,
                "mtime": mtime,
                "lang": file_extension,
                "nodes_to_index": nodes_to_index,
                "graph_nodes": list(graph_nodes_dict.values()),
                "graph_edges": graph_edges,
                "resolved_deps": resolved_deps
            }
        except Exception as e:
            import sys
            print(f"Warning: Failed to parse {file_str}: {e}", file=sys.stderr)
            return None

    def _post_process_recursion(self):
        """Find recursive self-calls and add RECURSES_TO relationships."""
        queries = [
            "MATCH (m:Method)-[:CALLS]->(m) MERGE (m)-[:RECURSES_TO]->(m)",
            "MATCH (f:Function)-[:CALLS]->(f) MERGE (f)-[:RECURSES_TO]->(f)"
        ]
        try:
            with self.graph:
                self.graph.query_batch(queries)
        except Exception as e:
            import sys
            print(f"Warning: Failed to post-process recursion: {e}", file=sys.stderr)

    def index_directory(self, dirpath: str, progress_callback=None) -> dict:
        import pathlib
        import hashlib
        from concurrent.futures import ThreadPoolExecutor
        
        path = pathlib.Path(dirpath).resolve()
        from src.core.constants import SUPPORTED_EXTENSIONS
        SUPPORTED = SUPPORTED_EXTENSIONS
        ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules", "third_party", "dataset", "build_test", "build-context"}
        files = []
        for r, d, fnames in os.walk(str(path)):
            d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
            for fname in fnames:
                fp = (pathlib.Path(r) / fname).resolve()
                if fp.suffix in SUPPORTED:
                    files.append(fp)
        
        # Estimate required RAM disk quota based on a 40x source-to-database size multiplier
        try:
            total_source_bytes = sum(fp.stat().st_size for fp in files)
        except Exception:
            total_source_bytes = 0
            
        projected_db_bytes = int(total_source_bytes * 40.0)
        # 1.5x safety buffer, with a minimum of 1 GB
        required_ramdisk_bytes = max(int(projected_db_bytes * 1.5), 1024 * 1024 * 1024)
        
        if progress_callback:
            progress_callback(0, len(files), f"Projected raw DB size: {projected_db_bytes / (1024*1024):.1f} MB (Allocating {required_ramdisk_bytes / (1024*1024):.1f} MB safe quota)")

        indexed_count = 0
        skipped_count = 0
        current_files_set = set()
        
        # 1. Filter out files that have not changed
        parse_jobs = []
        for idx, fp in enumerate(files):
            file_str = str(fp)
            current_files_set.add(file_str)
            try:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                mtime = os.path.getmtime(file_str)
                
                existing_hash = self.search_index.get_file_hash(file_str)
                if existing_hash == content_hash:
                    skipped_count += 1
                    continue
                
                parse_jobs.append((fp, file_str, content_hash, mtime))
            except Exception as e:
                print(f"Warning: Failed to scan hash for {file_str}: {e}", file=sys.stderr)

        # 2. Parse AST of modified files concurrently
        max_workers = min(8, os.cpu_count() or 4)
        results = []
        if parse_jobs:
            if progress_callback:
                progress_callback(0, len(parse_jobs), "Starting parallel parsing...")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(self._parse_file_task, job[0], job[1], job[2], job[3])
                    for job in parse_jobs
                ]
                for idx, fut in enumerate(futures):
                    res = fut.result()
                    if res:
                        results.append(res)
                    if progress_callback:
                        progress_callback(idx + 1, len(parse_jobs), f"Parsed {idx+1}/{len(parse_jobs)}")

        if not results:
            # Nothing to index — still clean up stale files
            tracked_files = self.search_index.get_all_tracked_files()
            stale_files = [tf for tf in tracked_files if tf not in current_files_set]
            stale_count = len(stale_files)
            if stale_files:
                if progress_callback:
                    progress_callback(len(files), len(files), f"Removing {stale_count} stale files...")
                with self.graph:
                    self.search_index.clear_files_bulk(stale_files)
            return {
                "status": "success",
                "indexed_files": 0,
                "skipped_files": skipped_count,
                "stale_files_removed": stale_count,
                "total_files_found": len(files)
            }

        # 3. Build the index in RAM (/dev/shm), then sync to SSD
        ssd_db_path = self.search_index.db_path
        ramdisk = RamdiskIndex(ssd_db_path, max_bytes=required_ramdisk_bytes)

        with ramdisk:
            # Create fresh DB instances pointing at the ramdisk
            ram_search = CodeSearchIndex(ramdisk.db_path)
            ram_graph = ram_search.graph  # Already initialized by CodeSearchIndex.__init__

            if progress_callback:
                progress_callback(0, len(results), "Writing parsed ASTs to RAM database...")

            with ram_graph:
                # Clear files in bulk (in RAM DB — fast, no SSD I/O)
                files_to_clear = [res["file_str"] for res in results]
                if files_to_clear:
                    if progress_callback:
                        progress_callback(0, len(results), "Clearing existing indexes for modified files...")
                    ram_search.clear_files_bulk(files_to_clear)

                # Aggregate all data
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
                    
                    from src.core.constants import get_language_for_extension
                    lang_name = get_language_for_extension(res["lang"])
                    files_metadata.append((file_str, res["content_hash"], res["mtime"], lang_name))

                # Perform bulk saving — all to RAM
                try:
                    if all_search_nodes:
                        if progress_callback:
                            progress_callback(30, 100, "Saving search nodes to RAM DuckDB...")
                        ram_search.index_nodes(all_search_nodes)

                    if all_graph_nodes:
                        if progress_callback:
                            progress_callback(50, 100, "Inserting graph nodes into RAM Gorgonzola...")
                        nodes_list = [(nid, props, lbl) for nid, (props, lbl) in all_graph_nodes.items()]
                        id_map = ram_graph.insert_nodes_bulk(nodes_list)
                        
                        if all_graph_edges:
                            if progress_callback:
                                progress_callback(70, 100, "Linking graph edges in RAM...")
                            edges_list = [(src, dst, dict(props), rel) for src, dst, props, rel in all_graph_edges]
                            ram_graph.insert_edges_bulk(edges_list, id_map)

                    # Check quota after heavy writes
                    ramdisk.check_quota()
                    usage_mb = ramdisk.get_usage_bytes() / 1024 / 1024
                    print(f"[ramdisk] Current usage: {usage_mb:.2f} MB", file=sys.stderr, flush=True)

                    if files_metadata:
                        if progress_callback:
                            progress_callback(90, 100, "Updating file hash tracking in RAM...")
                        ram_search.upsert_file_hashes_bulk(files_metadata)
                        
                    indexed_count = len(results)
                except RamdiskQuotaExceeded as e:
                    print(f"[ramdisk] QUOTA EXCEEDED: {e}", file=sys.stderr, flush=True)
                    raise
                except Exception as e:
                    print(f"Warning: Failed during bulk database write: {e}", file=sys.stderr)

            # Close RAM DB connections before sync
            ram_search.close()

        # After ramdisk context exits, files are on SSD.
        # Re-open the SSD-backed connections for cleanup work.
        self.search_index.close()
        self.search_index = CodeSearchIndex(ssd_db_path)
        self.graph = self.search_index.graph

        # Clean up stale files that no longer exist on disk
        tracked_files = self.search_index.get_all_tracked_files()
        stale_files = [tf for tf in tracked_files if tf not in current_files_set]
        stale_count = len(stale_files)
        if stale_files:
            if progress_callback:
                progress_callback(len(files), len(files), f"Removing {stale_count} stale files...")
            with self.graph:
                self.search_index.clear_files_bulk(stale_files)
                
        self._post_process_recursion()
         
        if progress_callback:
            progress_callback(len(files), len(files), "Resolving symbols")
 
        # Resolve all unlinked AST symbols (CALLS, EXTENDS, IMPLEMENTS)
        from src.mcp_server.graph_api import GraphAPI
        graph_api = GraphAPI(dirpath)
        graph_api.resolve_symbols()
        
        # Rebuild the FTS index for the whole directory once
        try:
            self.search_index.rebuild_fts()
        except Exception as e:
            print(f"Warning: Failed to rebuild FTS index for directory: {e}", file=sys.stderr)
        
        return {
            "status": "success", 
            "indexed_files": indexed_count, 
            "skipped_files": skipped_count,
            "stale_files_removed": stale_count,
            "total_files_found": len(files)
        }

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
        sys.exit(1)

if __name__ == "__main__":
    main()

