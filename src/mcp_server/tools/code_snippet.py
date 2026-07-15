import asyncio
import logging
import os
from typing import Optional

from src.core.errors import AnalysisError, IndexNotFoundError, SecurityValidationError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path
from mcp.server import ServerRequestContext

logger = logging.getLogger(__name__)

async def get_code_snippet(
    target: str,
    symbol: str,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    """Fetch the full source code for a specific symbol (function, class, etc.)."""
    if not symbol:
        raise SecurityValidationError("Symbol name is required")

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
        )

    index = _get_cached_api(repo_root, db_path, "index")

    def _fetch_snippet():
        conn = index._conn
        # Search by exact name match first or fallback to LIKE
        res = conn.execute(
            """
            SELECT id, name, kind, filepath, start_line, end_line 
            FROM nodes 
            WHERE name = ? OR id = ?
            LIMIT 10
            """,
            [symbol, symbol]
        ).fetchall()
        
        if not res:
            res = conn.execute(
                """
                SELECT id, name, kind, filepath, start_line, end_line 
                FROM nodes 
                WHERE name LIKE ?
                LIMIT 10
                """,
                [f"%{symbol}"]
            ).fetchall()

        if not res:
            return None

        results = []
        for r in res:
            node_id, name, kind, filepath, start_line, end_line = r
            body_text = index._lazy_load_body(filepath, start_line, end_line)
            results.append({
                "id": node_id,
                "name": name,
                "kind": kind,
                "filepath": filepath,
                "start_line": start_line,
                "end_line": end_line,
                "body_text": body_text
            })
        return results

    results = await asyncio.to_thread(_fetch_snippet)
    
    if not results:
        return {"status": "not_found", "message": f"Symbol '{symbol}' not found in index."}

    return {
        "status": "ok",
        "symbol": symbol,
        "results": results
    }
