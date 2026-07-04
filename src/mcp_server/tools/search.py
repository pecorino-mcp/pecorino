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
from mcp.server import ServerRequestContext

async def do_search(target: str, query: str, limit: int = 10, offset: int = 0, output_file: Optional[str] = None, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    if not query:
        raise SecurityValidationError("Query is required for search tool")
        
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

    if not output_file:
        for r in results:
            r.pop("body_text", None)
            
    return {"query": query, "results": results, "search_status": "ok"}
