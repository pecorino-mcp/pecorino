import asyncio
import logging
import threading
from typing import Optional

from src.core.errors import AnalysisError, SecurityValidationError, IndexNotFoundError
from src.mcp_server.middleware.caching import _get_cached_api, clear_index_cache
from src.mcp_server.middleware.security import safe_path, check_suspicious
from src.mcp_server.middleware.sync import _auto_sync_stale

logger = logging.getLogger(__name__)

_fts_rebuild_lock = threading.Lock()
INDEX_TIMEOUT_S = 300
MAX_QUERY_LEN = 200
MAX_LIMIT = 100
MAX_CODE_LINES = 300
from mcp.server import ServerRequestContext

def _cap_body(body: str) -> str:
    """Truncate body_text to MAX_CODE_LINES lines."""
    if not body:
        return ''
    lines = body.split('\n')
    if len(lines) > MAX_CODE_LINES:
        return '\n'.join(lines[:MAX_CODE_LINES]) + f"\n... (truncated at {MAX_CODE_LINES} lines)"
    return body

async def do_search(target: str, query: Optional[str] = None, limit: int = 10, offset: int = 0, output_file: Optional[str] = None, allow_external: bool = False, include_source: bool = False, auto_expand_source: bool = True, ctx: Optional[ServerRequestContext] = None) -> dict:
    """Unified search and code retrieval tool.
    
    When include_source=False (default): returns metadata-only search results.
    When include_source=True: returns results with source code (body_text), capped at 300 lines.
    When target is a file: returns nodes from that file (query is optional filter).
    When target is a directory: query is required for FTS search.
    
    Auto-expansion: when ≤3 results and auto_expand_source=True (default),
    source code is automatically included to reduce round-trips.
    """
    if query:
        query = query.strip()[:MAX_QUERY_LEN]
        if any(c in query for c in "\x00\n\r"):
            raise SecurityValidationError("Invalid characters in query")
        check_suspicious(query, "query")
        
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)
    
    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    index = _get_cached_api(repo_root, db_path, "index")

    # --- File target: return nodes directly ---
    if path.is_file():
        nodes = await asyncio.to_thread(index.get_file_nodes, str(path))
        if query:
            q_lower = query.lower()
            filtered_nodes = []
            for n in nodes:
                if q_lower in n['name'].lower():
                    filtered_nodes.append(n)
                elif q_lower in n.get('body_text', '').lower():
                    filtered_nodes.append(n)
            nodes = filtered_nodes
        nodes = nodes[offset:offset+limit]

        # Auto-expand: include source when few results
        auto_expanded = False
        effective_include = include_source
        if not include_source and auto_expand_source and len(nodes) <= 3:
            effective_include = True
            auto_expanded = True

        if effective_include:
            for n in nodes:
                n['body_text'] = _cap_body(n.get('body_text', ''))
        else:
            for n in nodes:
                n.pop('body_text', None)
        result = {"query": query, "results": nodes, "search_status": "ok"}
        if auto_expanded:
            result["auto_expanded"] = True
        return result

    # --- Directory target: FTS search ---
    if not query:
        raise SecurityValidationError(
            "Query is required when searching a directory",
            suggestion="Provide a search query string, or target a specific file path instead.",
        )

    # Lazy FTS rebuild if stale
    if not index.has_fts_index() or index.is_fts_dirty():
        from src.mcp_server.index_db import CodeSearchIndex

        def _rebuild_fts():
            write_index = CodeSearchIndex(db_path=db_path, read_only=False)
            try:
                write_index.ensure_fts()
            finally:
                write_index.close()

        with _fts_rebuild_lock:
            if not index.has_fts_index() or index.is_fts_dirty():
                logger.info("Lazy FTS rebuild triggered for %s", db_path)
                clear_index_cache()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_rebuild_fts),
                        timeout=INDEX_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    raise AnalysisError(
                        f"FTS rebuild timed out after {INDEX_TIMEOUT_S}s. "
                        f"Run 'update_index' manually to rebuild."
                    )
                clear_index_cache()
                index = _get_cached_api(repo_root, db_path, "index")
                
    results = await asyncio.to_thread(index.search, query, limit, path.as_posix(), offset)

    # Auto-expand: include source when few results
    auto_expanded = False
    if not include_source and auto_expand_source and len(results) <= 3 and not output_file:
        include_source = True
        auto_expanded = True

    if include_source:
        for r in results:
            r['body_text'] = _cap_body(r.get('body_text', ''))
    elif not output_file:
        for r in results:
            r.pop("body_text", None)

    result = {"query": query, "results": results, "search_status": "ok"}
    if auto_expanded:
        result["auto_expanded"] = True
    return result
