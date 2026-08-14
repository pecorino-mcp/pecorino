import logging
import re
from typing import Any, Optional

from mcp.server import ServerRequestContext

from src.core.errors import AnalysisError, IndexNotFoundError, SecurityValidationError

logger = logging.getLogger(__name__)

async def do_query_graph(
    target: str,
    query: str,
    parameters: Optional[dict[str, Any]] = None,
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Execute an openCypher query against the Kùzu graph."""

    # Natural Language Detection and Text-to-Cypher translation
    upper_query = query.upper()
    is_cypher = any(kw in upper_query for kw in ["MATCH ", "RETURN ", "WITH ", "CALL "])
    
    if not is_cypher:
        from src.mcp_server.llm_client import generate_cypher
        from src.mcp_server.context_helper import PecorinoContext
        schema = """
- Node CodeNode (id STRING, kind STRING, name STRING, qualified_name STRING, file STRING, line INT64, docstring STRING)
- Node File (id STRING, name STRING, path STRING)
- Rel CALLS (FROM CodeNode TO CodeNode)
- Rel IN_FILE (FROM CodeNode TO File)
        """
        generated = await generate_cypher(query, schema, PecorinoContext(ctx))
        if generated:
            logger.info(f"Generated Cypher from NL: {generated}")
            query = generated
            upper_query = query.upper()
        else:
            raise AnalysisError("Failed to generate Cypher query from natural language.")

    # Basic Read-Only Check
    # This checks for mutating openCypher keywords to prevent accidental writes.
    mutating_keywords = ["CREATE", "MERGE", "SET", "DELETE", "REMOVE", "DROP"]
    upper_query = query.upper()
    for kw in mutating_keywords:
        if kw in upper_query:
            if re.search(rf"\b{kw}\b", upper_query):
                raise SecurityValidationError(
                    f"Mutation operations are not allowed in query_graph. Keyword {kw} detected.",
                    valid_values=["Read-only openCypher queries: MATCH, RETURN, WITH, CALL"],
                    suggestion="Use read-only queries only."
                )

    from pathlib import Path
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    from src.mcp_server.middleware.security import safe_path

    path = safe_path(target, allow_external)
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    if not Path(db_path).exists():
        raise IndexNotFoundError("Graph index not found or uninitialized. Run update_index first.")

    from src.mcp_server.middleware.caching import _get_cached_api
    graph_api = _get_cached_api(repo_root, db_path, "graph")
    if not graph_api:
        raise IndexNotFoundError("Graph index not found or uninitialized. Run update_index first.")

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
        raise AnalysisError(f"openCypher query execution failed: {e}") from e

