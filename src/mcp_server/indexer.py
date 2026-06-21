import os
import json
import asyncio
from typing import Dict, Any, List

from src.mcp_server.index import CodeSearchIndex, get_db_path_for_repo, find_repo_root
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

from src.parsers.ast import ClassDef, InterfaceDef, walk
from src.parsers.tree_sitter_parser import parse_with_tree_sitter

class CodebaseIndexer:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        db_path = get_db_path_for_repo(self.repo_path)
        
        self.search_index = CodeSearchIndex(db_path)
        self.graph = GorgonzolaGraph(db_path=db_path)

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

    def index_file(self, filepath: str, content: str, file_extension: str):
        """Parse and index a single file for search and dependency graph."""
        tree = parse_with_tree_sitter(content, file_extension)
        if not tree:
            return
            
        self.search_index.clear_file(filepath)
        
        # Clear graph database nodes for this file
        try:
            self.graph.query("MATCH (f:File {id: $id})-[r1:CONTAINS]->(c:Class)-[r2:CONTAINS]->(m:Method) DETACH DELETE m", {"id": filepath})
            self.graph.query("MATCH (f:File {id: $id})-[r1:CONTAINS]->(i:Interface)-[r2:CONTAINS]->(m:Method) DETACH DELETE m", {"id": filepath})
            self.graph.query("MATCH (f:File {id: $id})-[r:CONTAINS]->(child) DETACH DELETE child", {"id": filepath})
            self.graph.query("MATCH (f:File {id: $id}) DETACH DELETE f", {"id": filepath})
        except Exception:
            pass

        content_lines = content.splitlines()
        nodes_to_index = []
        
        graph_nodes_dict = {}
        graph_edges = []
        
        file_id = filepath
        graph_nodes_dict[file_id] = (file_id, {"name": os.path.basename(filepath), "path": filepath, "extension": file_extension}, "File")

        dependencies = set()

        for node in walk(tree):
            if isinstance(node, ClassDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                # Get metrics with defaults since we don't have CK metrics calculator
                wmc = getattr(node, 'wmc', 0)
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'class',
                    'filepath': filepath,
                    'body_text': body,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'metrics': {'wmc': wmc, 'cbo': getattr(node, 'cbo', 0), 'rfc': getattr(node, 'rfc', 0), 'lcom': getattr(node, 'lcom', 0)}
                })
                
                class_id = f"{filepath}::{node.name}"
                graph_nodes_dict[class_id] = (class_id, {"name": node.name}, "Class")
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
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    m_cc = getattr(m, 'cyclomatic_complexity', 1)
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'body_text': m_body,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': {'cyclomatic_complexity': m_cc}
                    })
                    method_id = f"{class_id}::{m.name}"
                    graph_nodes_dict[method_id] = (method_id, {"name": m.name, "complexity": m_cc}, "Method")
                    graph_edges.append((class_id, method_id, {}, "CONTAINS"))
                    
                    for call in getattr(m, 'called_methods', set()):
                        symbol_id = f"Symbol::{call}"
                        graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                        graph_edges.append((method_id, symbol_id, {}, "CALLS"))
            elif isinstance(node, InterfaceDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'interface',
                    'filepath': filepath,
                    'body_text': body,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'metrics': {}
                })
                class_id = f"{filepath}::{node.name}"
                graph_nodes_dict[class_id] = (class_id, {"name": node.name}, "Interface")
                graph_edges.append((file_id, class_id, {}, "CONTAINS"))
                for m in node.methods:
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    m_cc = getattr(m, 'cyclomatic_complexity', 1)
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'body_text': m_body,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': {'cyclomatic_complexity': m_cc}
                    })
                    method_id = f"{class_id}::{m.name}"
                    graph_nodes_dict[method_id] = (method_id, {"name": m.name, "complexity": m_cc}, "Method")
                    graph_edges.append((class_id, method_id, {}, "CONTAINS"))
            elif type(node).__name__ == 'ImportDef':
                if node.module:
                    dependencies.add(node.module)

        for func in tree.functions:
            f_body = '\n'.join(content_lines[max(0, func.lineno-1):func.end_lineno]) if func.lineno > 0 else ''
            f_cc = getattr(func, 'cyclomatic_complexity', 1)
            nodes_to_index.append({
                'name': func.name,
                'node_type': 'function',
                'filepath': filepath,
                'body_text': f_body,
                'start_line': func.lineno,
                'end_line': func.end_lineno,
                'metrics': {'cyclomatic_complexity': f_cc}
            })
            func_id = f"{filepath}::{func.name}"
            graph_nodes_dict[func_id] = (func_id, {"name": func.name, "complexity": f_cc}, "Function")
            graph_edges.append((file_id, func_id, {}, "CONTAINS"))
            
            for call in getattr(func, 'called_methods', set()):
                symbol_id = f"Symbol::{call}"
                graph_nodes_dict[symbol_id] = (symbol_id, {"name": call}, "Symbol")
                graph_edges.append((func_id, symbol_id, {}, "CALLS"))
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

    def index_directory(self, dirpath: str, progress_callback=None) -> dict:
        import pathlib
        import hashlib
        path = pathlib.Path(dirpath)
        SUPPORTED = {'.py','.pyi','.java','.scala','.kt','.js','.jsx','.ts','.tsx',
                     '.cpp','.cc','.cxx','.c','.h','.hpp','.hxx','.go','.rs','.swift'}
        ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules"}
        files = []
        for r, d, fnames in os.walk(str(path)):
            d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
            for fname in fnames:
                fp = pathlib.Path(r) / fname
                if fp.suffix in SUPPORTED:
                    files.append(fp)
        
        indexed_count = 0
        skipped_count = 0
        current_files_set = set()
        
        for idx, fp in enumerate(files):
            file_str = str(fp)
            current_files_set.add(file_str)
            if progress_callback:
                progress_callback(idx, len(files), file_str)
            try:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                mtime = os.path.getmtime(file_str)
                
                # Check if file has changed
                existing_hash = self.search_index.get_file_hash(file_str)
                if existing_hash == content_hash:
                    skipped_count += 1
                    continue
                    
                self.index_file(file_str, content, fp.suffix)
                
                # Update hash in tracking table
                from src.core.constants import get_language_for_extension
                lang = get_language_for_extension(fp.suffix)
                self.search_index.upsert_file_hash(file_str, content_hash, mtime, lang)
                
                indexed_count += 1
            except Exception as e:
                import sys
                print(f"Warning: Failed to index {fp}: {e}", file=sys.stderr)
                
        # Clean up stale files that no longer exist on disk
        tracked_files = self.search_index.get_all_tracked_files()
        stale_count = 0
        for tf in tracked_files:
            if tf not in current_files_set:
                self.search_index.clear_file(tf)
                stale_count += 1
                
        # Note: Optimize is intentionally left out here to prevent 
        # blocking database queries with a full vacuum/merge during normal ingestion.
        
        if progress_callback:
            progress_callback(len(files), len(files), "Resolving symbols")

        # Resolve all unlinked AST symbols (CALLS, EXTENDS, IMPLEMENTS)
        from src.mcp_server.graph_api import GraphAPI
        graph_api = GraphAPI(dirpath)
        graph_api.resolve_symbols()
        
        return {
            "status": "success", 
            "indexed_files": indexed_count, 
            "skipped_files": skipped_count,
            "stale_files_removed": stale_count,
            "total_files_found": len(files)
        }

