import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional

from src.core.errors import SecurityValidationError, IndexNotFoundError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path, check_suspicious
from src.mcp_server.dsl.compiler import DSLCompiler
from src.mcp_server.prometheus_metrics import GRAPH_DB_SIZE
from src.mcp_server.index_db import get_graph_path_for_repo

logger = logging.getLogger(__name__)
from mcp.server import ServerRequestContext

# ── Intent-based presets ──────────────────────────────────────
# The server translates these into correct DSL queries so LLMs
# don't need to know the exact JSON schema.

INTENT_PRESETS: dict[str, dict] = {
    "all_classes": {
        "select": "nodes",
        "where": {"node_type": {"in": ["class", "interface"]}},
        "limit": 50,
    },
    "all_functions": {
        "select": "nodes",
        "where": {"node_type": {"in": ["function", "method"]}},
        "limit": 50,
    },
    "files_by_language": {
        "select": "files",
        "limit": 100,
    },
    # Graph-dependent intents (handled specially in do_query)
    "entry_points": {
        "_graph_intent": "entry_points",
        "select": "nodes",
        "where": {"node_type": {"in": ["function", "method"]}},
        "limit": 20,
    },
    "dead_code": {
        "_graph_intent": "dead_code",
        "select": "nodes",
        "where": {"node_type": {"in": ["function", "method"]}},
        "limit": 50,
    },
}

async def do_query(target: str, query_json: str | Dict[str, Any], allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    if isinstance(query_json, str):
        try:
            query_json = json.loads(query_json)
        except json.JSONDecodeError:
            raise SecurityValidationError(
                "Invalid JSON in query_json parameter",
                suggestion="Provide valid JSON, or use the 'intent' parameter instead for common queries.",
            )
            
    if not isinstance(query_json, dict):
        raise SecurityValidationError(
            "query_json must be a JSON object",
            suggestion="Provide a JSON object like {\"select\": \"nodes\", \"where\": {\"node_type\": \"function\"}}",
        )

    # Extract and strip internal graph intent marker
    graph_intent = query_json.pop("_graph_intent", None)

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

    # ── Graph-dependent intents ───────────────────────────────
    if graph_intent in ("entry_points", "dead_code"):
        try:
            graph_api = _get_cached_api(repo_root, db_path, "graph")
        except Exception:
            return {
                "status": "error",
                "error_type": "index_missing",
                "message": "Graph index not available for this repository.",
                "suggestion": "Run 'update_index' first to build the graph, then retry this intent.",
            }

        index = _get_cached_api(repo_root, db_path, "index")
        # Get all function/method nodes
        limit = min(query_json.get("limit", 50), 100)
        nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        fn_nodes = [n for n in nodes if n.get("node_type") in ("function", "method")]

        # Build a caller count map from the graph in one query
        caller_counts = {}
        try:
            cypher = '''
            MATCH (caller)-[:CALLS]->(callee)
            RETURN callee.name AS name, COUNT(caller) AS caller_count
            '''
            rows = await asyncio.to_thread(graph_api.graph.query, cypher)
            for r in rows:
                if r.get('name'):
                    caller_counts[r['name']] = r['caller_count']
        except Exception as e:
            logger.warning(f"Bulk caller count query failed: {e}")

        results = []
        for node in fn_nodes:
            name = node.get("name", "")
            caller_count = caller_counts.get(name, 0)

            if graph_intent == "entry_points" and caller_count >= 5:
                results.append({**node, "caller_count": caller_count})
            elif graph_intent == "dead_code" and caller_count == 0:
                results.append({**node, "caller_count": 0})

        # Sort entry_points by most-called first
        if graph_intent == "entry_points":
            results.sort(key=lambda x: x.get("caller_count", 0), reverse=True)

        # Strip body_text for token efficiency
        for r in results:
            r.pop("body_text", None)

        return {
            "status": "ok",
            "intent": graph_intent,
            "results": results[:limit],
            "total_candidates": len(fn_nodes),
        }

    # ── Standard DSL path ─────────────────────────────────────
    sql_query, cypher_query, sql_params = DSLCompiler.compile(query_json, db_path="main")
    
    index = _get_cached_api(repo_root, db_path, "index")
    conn = index._conn
    
    results = []
    
    if cypher_query:
        # If there's a graph join, we need to query Kuzu first to get the matching node IDs
        graph_api = _get_cached_api(repo_root, db_path, "graph")
        graph_res = await asyncio.to_thread(graph_api.graph.query, cypher_query)
        
        # Track graph DB size
        def get_dir_size(path: str) -> int:
            total = 0
            try:
                for dirpath, _, filenames in os.walk(path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if not os.path.islink(fp):
                            total += os.path.getsize(fp)
            except Exception as e:
                logger.warning(f"Failed to get graph db size: {e}")
            return total
            
        graph_db_dir = get_graph_path_for_repo(db_path)
        if os.path.exists(graph_db_dir):
            GRAPH_DB_SIZE.set(get_dir_size(graph_db_dir))
            
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
