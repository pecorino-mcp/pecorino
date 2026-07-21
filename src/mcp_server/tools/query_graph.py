import logging
from typing import Optional, Any
from mcp.server import ServerRequestContext

from src.core.errors import AnalysisError

logger = logging.getLogger(__name__)

async def do_query_graph(
    target: str,
    query: str,
    parameters: Optional[dict[str, Any]] = None,
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Execute an openCypher query against the Kùzu graph."""
    
    # Basic Read-Only Check
    # This checks for mutating openCypher keywords to prevent accidental writes.
    mutating_keywords = ["CREATE", "MERGE", "SET", "DELETE", "REMOVE", "DROP"]
    upper_query = query.upper()
    for kw in mutating_keywords:
        if kw in upper_query:
            # simple check: if it contains the word bordered by spaces or start/end
            import re
            if re.search(rf"\b{kw}\b", upper_query):
                return {
                    "status": "error", 
                    "message": f"Mutation operations are not allowed in query_graph. Keyword {kw} detected."
                }

    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    from src.mcp_server.middleware.security import safe_path
    
    path = safe_path(target, allow_external)
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    from src.mcp_server.middleware.caching import _get_cached_api
    graph_api = _get_cached_api(repo_root, db_path, "graph")
    if not graph_api:
        return {"status": "error", "message": "Graph index not found or uninitialized. Run update_index first."}

    import re
    # Neo4j compatibility: Kùzu uses LABEL() instead of type() for relationships
    # We use uppercase LABEL() to bypass a naive regex in Gorgonzola that replaces lowercase label() with .kind
    query = re.sub(r'(?i)\btype\s*\(', 'LABEL(', query)

    try:
        results = graph_api.graph.query(query, parameters or {})
        return {
            "status": "success",
            "results": results,
            "count": len(results) if isinstance(results, list) else 0
        }
    except Exception as e:
        logger.error(f"query_graph failed: {e}")
        return {"status": "error", "message": str(e)}
