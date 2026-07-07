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

ALLOWED_VIEWS = frozenset({"summary", "classes", "functions", "deps", "tree"})
MAX_LIMIT = 100
MAX_DEPTH = 10
MAX_QUERY_LEN = 200
MAX_CODE_LINES = 300
INDEX_TIMEOUT_S = 300
from mcp.server import ServerRequestContext

async def do_browse(target: str, view: str = "summary", query: Optional[str] = None, limit: int = 10, offset: int = 0, max_depth: int = 3, output_file: Optional[str] = None, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
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

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)
    
    # Require explicit indexing for external repositories
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    index = _get_cached_api(repo_root, db_path, "index")
    
    api = None
    if view in ("summary", "deps"):
        api = _get_cached_api(repo_root, db_path, "graph")


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
            # Directory: aggregate outgoing dependencies from the graph
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
        if is_dir:
            nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        else:
            nodes = await asyncio.to_thread(index.get_file_nodes, str(path))

        if view == "classes":
            type_filter = ('class', 'interface')
        else:
            type_filter = ('function', 'method')

        filtered = [
            {"name": n['name'], "filepath": n['filepath'], "line": n['start_line']}
            for n in nodes if n['node_type'] in type_filter
        ]
        result["structure"] = filtered

    else:  # summary
        if is_dir:
            nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
            indexed_files = len(set(n['filepath'] for n in nodes))

            # Capped on-disk scan
            on_disk_count = 0
            try:
                for root, dirs, fnames in os.walk(str(path)):
                    # Ignore common ignored dirs to match indexer
                    dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules", "third_party", "dataset", "build_test", "build-context"}]
                    on_disk_count += len(fnames)
                    if on_disk_count > 1000:
                        on_disk_count = "1000+"
                        break
            except Exception:
                on_disk_count = "unknown"

            # Language breakdown and total files from the database files table
            try:
                prefix = path.as_posix() if path.as_posix().endswith('/') else f"{path.as_posix()}/"
                db_res = index._conn.execute('''
                    SELECT lang, count(*)
                    FROM files
                    WHERE filepath LIKE ?
                    GROUP BY lang
                ''', (f"{prefix}%",)).fetchall()
                lang_breakdown = {row[0]: row[1] for row in db_res}
            except Exception:
                lang_breakdown = {}

            node_preview = [n['name'] for n in nodes[:20]]

            # Architectural grouping: cluster files by top-level subdirectory
            architectural_groups = {}
            try:
                prefix = path.as_posix() if path.as_posix().endswith('/') else f"{path.as_posix()}/"
                all_files = index._conn.execute('''
                    SELECT filepath FROM files WHERE filepath LIKE ?
                ''', (f"{prefix}%",)).fetchall()
                for (fp,) in all_files:
                    rel = fp[len(prefix):]
                    parts = rel.split("/")
                    group_name = parts[0] if len(parts) > 1 else "(root)"
                    if group_name not in architectural_groups:
                        architectural_groups[group_name] = {"file_count": 0, "path": f"{prefix}{group_name}/"}
                    architectural_groups[group_name]["file_count"] += 1
                # Sort by file count descending, keep top 15
                architectural_groups = dict(
                    sorted(architectural_groups.items(), key=lambda x: x[1]["file_count"], reverse=True)[:15]
                )
            except Exception:
                pass

            result["structure"] = {
                "indexed_files_count": indexed_files,
                "total_files_on_disk": on_disk_count,
                "language_breakdown": lang_breakdown,
                "architectural_groups": architectural_groups,
                "top_level_symbols_preview": node_preview
            }
        else:
            nodes = await asyncio.to_thread(index.get_file_nodes, str(path))
            classes = sum(1 for n in nodes if n['node_type'] in ('class', 'interface'))
            functions_nodes = [n for n in nodes if n['node_type'] in ('function', 'method')]

            top_level = 0
            nested = 0
            methods = 0

            # O(n log n) nesting detection: sort by (start_line, -end_line) to enable
            # single-pass scope tracking with a stack.
            sorted_nodes = sorted(nodes, key=lambda n: (n['start_line'], -n['end_line']))
            scope_stack = []
            for node in sorted_nodes:
                # Pop scopes that ended before this node starts
                while scope_stack and scope_stack[-1]['end_line'] < node['start_line']:
                    scope_stack.pop()
                if node['node_type'] in ('function', 'method'):
                    if not scope_stack:
                        top_level += 1
                    else:
                        innermost = scope_stack[-1]
                        if innermost['node_type'] in ('function', 'method'):
                            nested += 1
                        elif innermost['node_type'] in ('class', 'interface'):
                            methods += 1
                        else:
                            top_level += 1
                scope_stack.append(node)

            result["structure"] = {
                "classes": classes,
                "top_level_functions": top_level,
                "nested_functions": nested,
                "methods": methods,
            }

    # Graph database dependency and PageRank retrieval (file-level only)
    if view in ("summary", "deps") and is_file:
        try:
            graph_metrics = await asyncio.to_thread(api.get_file_dependencies, path.as_posix())
            if view == "summary":
                if "structure" in result and isinstance(result["structure"], dict):
                    result["structure"]["graph_metrics"] = graph_metrics
            else:  # view == "deps"
                result["graph_metrics"] = graph_metrics
        except Exception as e:
            logger.warning("Graph database query failed: %s", e)

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
            stale_count = len(index.get_stale_files(str(path)))
            if stale_count > 0:
                result["index_staleness"] = {
                    "stale_file_count": stale_count,
                    "hint": "Run update_index to refresh. Search/graph views auto-sync."
                }
        except Exception:
            pass

    return truncate_lists(result)

