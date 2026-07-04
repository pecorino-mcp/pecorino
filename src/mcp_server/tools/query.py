import asyncio
import json
import logging
from typing import Dict, Any, Optional

from src.core.errors import SecurityValidationError, IndexNotFoundError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path, check_suspicious
from src.mcp_server.dsl.compiler import DSLCompiler

logger = logging.getLogger(__name__)
from mcp.server import ServerRequestContext

async def do_query(target: str, query_json: str | Dict[str, Any], allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    if isinstance(query_json, str):
        try:
            query_json = json.loads(query_json)
        except json.JSONDecodeError:
            raise SecurityValidationError("Invalid JSON in query_json parameter")
            
    if not isinstance(query_json, dict):
        raise SecurityValidationError("query_json must be a JSON object")

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

    # Sync any stale files
    from src.mcp_server.middleware.sync import _auto_sync_stale
    await _auto_sync_stale(repo_root, db_path, str(path))

    sql_query, cypher_query, sql_params = DSLCompiler.compile(query_json, db_path="main")
    
    index = _get_cached_api(repo_root, db_path, "index")
    conn = index._conn
    
    results = []
    
    if cypher_query:
        # If there's a graph join, we need to query Kuzu first to get the matching node IDs
        graph_api = _get_cached_api(repo_root, db_path, "graph")
        graph_res = await asyncio.to_thread(graph_api.graph.query, cypher_query)
        # Extract node IDs
        matching_ids = []
        for row in graph_res:
            matching_ids.append(row['id'])
            
        if not matching_ids:
            return {"status": "ok", "results": [], "note": "No graph matches found"}
            
        # Add IN clause for IDs
        placeholders = ",".join(["?" for _ in matching_ids])
        # Inject the IN clause into the SQL query before the LIMIT/OFFSET
        limit_idx = sql_query.upper().rfind("LIMIT")
        if limit_idx != -1:
            sql_query = f"{sql_query[:limit_idx]} AND id IN ({placeholders}) {sql_query[limit_idx:]}"
        else:
            sql_query = f"{sql_query} AND id IN ({placeholders})"
            
        sql_params.extend(matching_ids)
        
    try:
        df = await asyncio.to_thread(conn.execute, sql_query, sql_params)
        columns = [desc[0] for desc in df.description]
        rows = df.fetchall()
        for r in rows:
            results.append(dict(zip(columns, r)))
    except Exception as e:
        logger.warning(f"DSL SQL query failed: {e}")
        return {"status": "error", "error": str(e), "sql": sql_query}

    return {
        "status": "ok",
        "query": query_json,
        "results": results
    }
