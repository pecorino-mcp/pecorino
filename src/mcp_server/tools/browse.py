import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional

from src.core.constants import SUPPORTED_EXTENSIONS as SUPPORTED
from src.core.errors import AnalysisError, IndexNotFoundError, SecurityValidationError
from src.mcp_server.middleware.caching import _get_cached_api, clear_index_cache
from src.mcp_server.middleware.security import (
    ALLOWED_OUTPUT,
    MAX_READ_BYTES,
    is_safe_path,
    read_limited,
    safe_output_path,
    safe_path,
)
from src.mcp_server.middleware.sync import _auto_sync_stale

logger = logging.getLogger(__name__)

_fts_rebuild_lock = threading.Lock()

ALLOWED_VIEWS = frozenset({"classes", "functions", "deps", "tree", "all", "pagerank", "summary"})
MAX_LIMIT = 100
MAX_DEPTH = 10
MAX_QUERY_LEN = 200
MAX_CODE_LINES = 300
INDEX_TIMEOUT_S = 300
from mcp.server import ServerRequestContext

async def do_browse(target: str, view: str = "tree", query: Optional[str] = None, limit: int = 10, offset: int = 0, max_depth: int = 3, output_file: Optional[str] = None, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    # --- Input validation ---
    view = view.strip().lower()
    if view not in ALLOWED_VIEWS:
        raise SecurityValidationError(
            f"Invalid view: '{view}'",
            valid_values=sorted(ALLOWED_VIEWS),
            suggestion="Use one of the listed view values.",
        )
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    if view == "all":
        views_to_query = ["classes", "functions", "deps", "tree", "pagerank"]
        tasks = [
            do_browse(target, v, query, limit, offset, max_depth, None, allow_external, ctx)
            for v in views_to_query
        ]
        sub_results = await asyncio.gather(*tasks)
        
        combined_structure = {}
        target_path = target
        target_type = "unknown"
        
        for v, res in zip(views_to_query, sub_results):
            target_path = res.get("target", target_path)
            target_type = res.get("type", target_type)
            if "structure" in res:
                combined_structure[v] = res["structure"]
            else:
                data = {k: val for k, val in res.items() if k not in ("target", "type", "view", "index_staleness")}
                if data:
                    combined_structure[v] = data
                    
        result = {
            "target": target_path,
            "type": target_type,
            "view": "all",
            "structure": combined_structure
        }
        
        if output_file:
            try:
                out_path = safe_output_path(output_file)
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2)
                return {"saved_to": str(out_path), "target": target_path, "type": target_type, "view": "all"}
            except Exception as e:
                logger.error("Failed to write browse output: %s", e)
                
        def truncate_lists(obj):
            if isinstance(obj, dict):
                return {k: truncate_lists(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                if len(obj) > limit:
                    truncated_list = [truncate_lists(v) for v in obj[:limit]]
                    truncated_list.append(f"... (truncated {len(obj) - limit} more items, use output_file parameter to view full results)")
                    return truncated_list
                return [truncate_lists(v) for v in obj]
            return obj
            
        return truncate_lists(result)

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)
    
    # Remove explicit index requirement
    try:
        index = _get_cached_api(repo_root, db_path, "index")
    except Exception:
        index = None
    
    api = None
    if view in ("deps", "pagerank"):
        try:
            api = _get_cached_api(repo_root, db_path, "graph")
        except Exception:
            api = None


    # ── Structural views: classes, functions, deps, tree, summary ──
    # Unified handling for both files and directories.
    is_dir = path.is_dir()
    is_file = path.is_file()

    if not is_dir and not is_file:
        raise SecurityValidationError(f"Target not found: {path}")

    if is_file and path.suffix not in SUPPORTED:
        raise SecurityValidationError(f"Unsupported extension: {path.suffix}")

    result: dict[str, Any] = {"target": path.as_posix(), "type": "directory" if is_dir else "file", "view": view}

    if view == "tree":
        if is_file:
            import tree_sitter

            from src.parsers.tree_sitter_parser import get_language_from_extension
            from src.parsers.tsgm import TreeSitterGrammarManager
            content = await asyncio.to_thread(read_limited, path)
            lang = TreeSitterGrammarManager().get_language(get_language_from_extension(path.suffix))
            parser = tree_sitter.Parser(lang)
            ts_tree = parser.parse(content.encode())
            tree_str = str(ts_tree.root_node)
            if not output_file and len(tree_str) > 10000:
                tree_str = tree_str[:10000] + "\n... (truncated for preview, use output_file parameter to save full AST)"
            result["structure"] = {"tree": tree_str}
        else:
            # Directory: indexed file listing grouped by language
            if index is None:
                file_entries = []
                try:
                    for root, dirs, fnames in os.walk(str(path)):
                        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules", "third_party", "dataset", "build_test", "build-context"}]
                        for fname in fnames:
                            file_entries.append({"path": os.path.relpath(os.path.join(root, fname), str(path)), "lang": "unknown"})
                            if len(file_entries) >= 1000:
                                break
                        if len(file_entries) >= 1000:
                            break
                except Exception:
                    pass
                result["structure"] = {"file_tree": file_entries, "total_files": len(file_entries), "note": "Dynamic scan (unindexed)"}
            else:
                try:
                    prefix = path.as_posix() if path.as_posix().endswith('/') else f"{path.as_posix()}/"
                    db_res = index._conn.execute('''
                        SELECT filepath, lang
                        FROM files
                        WHERE filepath LIKE ?
                        ORDER BY filepath
                    ''', (f"{prefix}%",)).fetchall()
                    file_entries = [{"path": row[0][len(prefix):], "lang": row[1]} for row in db_res]
                except Exception:
                    file_entries = []
                result["structure"] = {"file_tree": file_entries, "total_indexed_files": len(file_entries)}

    elif view == "deps":
        if is_file:
            content = await asyncio.to_thread(read_limited, path)
            tree = await asyncio.to_thread(parse, content, path.suffix)
            result["structure"] = [{"module": i.module, "names": i.names} for i in tree.imports]
        else:
            if index is None or api is None:
                raise IndexNotFoundError("Dependency view for unindexed directory requires running 'update_index' first.")
            prefix = path.as_posix() if path.as_posix().endswith('/') else f"{path.as_posix()}/"
            try:
                db_res = index._conn.execute('''
                    SELECT filepath FROM files WHERE filepath LIKE ?
                ''', (f"{prefix}%",)).fetchall()
                dir_files = [row[0] for row in db_res]
            except Exception:
                dir_files = []

            all_deps: dict[str, list] = {}
            for fp in dir_files:
                try:
                    file_deps = await asyncio.to_thread(api.get_file_dependencies, fp)
                    for dep in file_deps.get("outgoing_dependencies", []):
                        dep_id = dep["id"] if isinstance(dep, dict) else dep
                        if dep_id not in all_deps:
                            all_deps[dep_id] = []
                        if fp not in all_deps[dep_id]:
                            all_deps[dep_id].append(fp)
                except Exception:
                    continue

            result["structure"] = [
                {"dependency": dep_id, "depended_by": sources}
                for dep_id, sources in sorted(all_deps.items())
            ]

    elif view in ("classes", "functions"):
        if index is None:
            raise IndexNotFoundError(f"'{view}' view requires running 'update_index' first.")
        if is_dir:
            nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        else:
            nodes = await asyncio.to_thread(index.get_file_nodes, str(path))

        if view == "classes":
            type_filter = ('class', 'interface')
        else:
            type_filter = ('function', 'method')

        filtered = [
            {
                "name": n['name'], 
                "filepath": n['filepath'], 
                "line": n['start_line'],
                "start_byte": n.get('start_byte', 0),
                "end_byte": n.get('end_byte', 0)
            }
            for n in nodes if n['node_type'] in type_filter
        ]
        result["structure"] = filtered

    elif view == "pagerank":
        if index is None or api is None:
            raise IndexNotFoundError(f"'{view}' view requires running 'update_index' first.")
        if is_dir:
            pr_scores = []
            if api._pagerank_cache is None:
                pr_scores = await asyncio.to_thread(api.graph.pagerank)
                api._pagerank_cache = {pr.get("node_id"): pr.get("score", 0.0) for pr in pr_scores}
            
            prefix = path.as_posix()
            if not prefix.endswith('/'):
                prefix += '/'
                
            top_files = []
            for node_id, score in api._pagerank_cache.items():
                if node_id.startswith(prefix):
                    rel_path = node_id[len(prefix):]
                    top_files.append({"path": rel_path, "score": score})
            
            top_files.sort(key=lambda x: x["score"], reverse=True)
            result["structure"] = top_files
        else:
            try:
                graph_metrics = await asyncio.to_thread(api.get_file_dependencies, path.as_posix())
                result["structure"] = {"pagerank_score": graph_metrics.get("pagerank_score", 0.0)}
            except Exception as e:
                logger.warning("Graph database query failed: %s", e)
                result["structure"] = {"pagerank_score": 0.0}

    elif view == "summary":
        if is_file:
            result["structure"] = {"note": "Summary not available for files."}
        else:
            if index is not None:
                try:
                    prefix = path.as_posix() if path.as_posix().endswith('/') else f"{path.as_posix()}/"
                    db_res = index._conn.execute('''
                        SELECT COUNT(*) FROM files WHERE filepath LIKE ?
                    ''', (f"{prefix}%",)).fetchone()
                    total_files = db_res[0] if db_res else 0
                    result["structure"] = {"total_indexed_files": total_files, "note": "Summary overview."}
                except Exception:
                    result["structure"] = {"total_indexed_files": 0}
            else:
                result["structure"] = {"total_indexed_files": 0, "note": "Unindexed directory."}

    # Graph database dependency and PageRank retrieval (file-level only)
    if view in ("deps",) and is_file and api is not None:
        try:
            graph_metrics = await asyncio.to_thread(api.get_file_dependencies, path.as_posix())
            result["graph_metrics"] = graph_metrics
        except Exception as e:
            logger.warning("Graph database query failed: %s", e)

    if view == "tree":
        result["next_steps"] = "Use analyze(analysis='impact') on interesting files, or metrics(what=['hotspots']) for repo risk triage."
    elif view == "deps":
        result["next_steps"] = "Use analyze(analysis='callers') or explain_symbol to trace specific dependencies."
    elif view in ("classes", "functions"):
        result["next_steps"] = "Use search(query=name) or get_code_range to see the implementation."
    elif view == "pagerank":
        result["next_steps"] = "Use analyze(analysis='impact') on top-ranked files to understand critical dependencies."

    if not output_file and view in ("tree", "deps", "pagerank"):
        try:
            import time
            dumped = json.dumps(result)
            if len(dumped) > 50000:
                output_file = f".mcp_outputs/browse_{view}_{int(time.time())}.json"
        except Exception:
            pass

    if output_file:
        try:
            out_path = safe_output_path(output_file)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2)
            output_file = str(out_path)  # Use safe path in results
        except Exception as e:
            logger.error("Failed to write browse output: %s", e)

        # Create a summarized version of result to return to the LLM
        summary_result = {"saved_to": output_file}
        if "query" in result:
            summary_result["query"] = result["query"]
        if "target" in result:
            summary_result["target"] = result["target"]
        if "type" in result:
            summary_result["type"] = result["type"]

        if "results" in result:
            summary_result["results_summary"] = [
                {k: v for k, v in r.items() if k != "body_text"}
                for r in result["results"]
            ]
        elif "structure" in result:
            struct = result["structure"]
            if isinstance(struct, list):
                summary_result["structure_summary_count"] = len(struct)
                summary_result["structure_preview"] = struct[:5]
            elif isinstance(struct, dict):
                if "tree" in struct:
                    summary_result["structure_preview"] = {"tree_length": len(struct["tree"])}
                else:
                    summary_result["structure"] = struct
            else:
                summary_result["structure"] = struct
        else:
            for k, v in result.items():
                if k not in summary_result:
                    if isinstance(v, list):
                        summary_result[f"{k}_summary_count"] = len(v)
                        summary_result[f"{k}_preview"] = v[:5]
                    else:
                        summary_result[k] = v

        return summary_result

    # Truncate lists to the specified limit to prevent token explosion if not writing to file
    def truncate_lists(obj):
        if isinstance(obj, dict):
            return {k: truncate_lists(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            if len(obj) > limit:
                truncated_list = [truncate_lists(v) for v in obj[:limit]]
                truncated_list.append(f"... (truncated {len(obj) - limit} more items, use output_file parameter to view full results)")
                return truncated_list
            return [truncate_lists(v) for v in obj]
        return obj

    # Signal staleness for structure views that skipped auto-sync,
    # so the LLM can decide whether to call update_index first.
    _SYNC_VIEWS_SET = frozenset({"search", "code", "callers", "callees", "impact", "pagerank", "functional-analysis"})
    if view not in _SYNC_VIEWS_SET:
        try:
            if index is not None:
                stale_count = len(index.get_stale_files(str(path)))
                if stale_count > 0:
                    result["index_staleness"] = {
                        "stale_file_count": stale_count,
                        "hint": "Run update_index to refresh. Search/graph views auto-sync."
                    }
        except Exception:
            pass

    return truncate_lists(result)

