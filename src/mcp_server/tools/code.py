import asyncio
import logging
from typing import Optional

from src.core.errors import SecurityValidationError, IndexNotFoundError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path, check_suspicious
from src.mcp_server.middleware.sync import _auto_sync_stale

logger = logging.getLogger(__name__)

MAX_CODE_LINES = 300
MAX_QUERY_LEN = 200
MAX_LIMIT = 100

def _cap_body(body: str) -> str:
    """Truncate body_text to MAX_CODE_LINES lines."""
    if not body:
        return ''
    lines = body.split('\n')
    if len(lines) > MAX_CODE_LINES:
        return '\n'.join(lines[:MAX_CODE_LINES]) + f"\n... (truncated at {MAX_CODE_LINES} lines)"
    return body
from mcp.server import ServerRequestContext

async def do_get_code(target: str, query: Optional[str] = None, limit: int = 10, offset: int = 0, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
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

    if path.is_file():
        nodes = await asyncio.to_thread(index.get_file_nodes, str(path))
        if query:
            q_lower = query.lower()
            nodes = [n for n in nodes if q_lower in n['name'].lower()]
        
        # Apply offset and limit
        nodes = nodes[offset:offset+limit]
        for n in nodes:
            n['body_text'] = _cap_body(n.get('body_text', ''))
        results = nodes
    elif path.is_dir():
        if not query:
            raise SecurityValidationError("Query is required for code view on directories")
        results = await asyncio.to_thread(index.search, query, limit, path.as_posix(), offset)
        for r in results:
            r['body_text'] = _cap_body(r.get('body_text', ''))
    else:
        raise SecurityValidationError(f"Target not found: {path}")

    return {"query": query, "results": results, "code_status": "ok"}
