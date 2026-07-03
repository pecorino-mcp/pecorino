import asyncio
import logging
from typing import Optional

from src.core.errors import AnalysisError, SecurityValidationError, IndexNotFoundError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path, check_suspicious
from src.mcp_server.middleware.sync import _auto_sync_stale

logger = logging.getLogger(__name__)

MAX_QUERY_LEN = 200
MAX_LIMIT = 100
MAX_DEPTH = 10
ALLOWED_ANALYSES = frozenset({"callers", "callees", "impact", "pagerank", "functional-analysis"})
from mcp.server import ServerRequestContext

async def do_analyze(target: str, analysis: str, symbol: Optional[str] = None, limit: int = 10, offset: int = 0, max_depth: int = 3, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    analysis = analysis.strip().lower()
    if analysis not in ALLOWED_ANALYSES:
        raise SecurityValidationError(f"Invalid analysis type: {analysis}")

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    max_depth = max(1, min(int(max_depth), MAX_DEPTH))

    if symbol:
        symbol = symbol.strip()[:MAX_QUERY_LEN]
        if any(c in symbol for c in "\x00\n\r"):
            raise SecurityValidationError("Invalid characters in symbol")
        check_suspicious(symbol, "symbol")
        
    if analysis in ("callers", "callees") and not symbol:
        raise SecurityValidationError(f"Symbol name is required for {analysis} analysis")

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
    api = _get_cached_api(repo_root, db_path, "graph")

    if analysis == "callers":
        callers = await asyncio.to_thread(api.find_callers, symbol)
        return {"target": symbol, "analysis": analysis, "callers": callers[offset:offset+limit]}

    if analysis == "callees":
        callees = await asyncio.to_thread(api.find_callees, symbol)
        return {"target": symbol, "analysis": analysis, "callees": callees[offset:offset+limit]}

    if analysis == "impact":
        deps = await asyncio.to_thread(api.impact_analysis, path.as_posix(), max_depth)
        return {"target": path.as_posix(), "analysis": analysis, "dependent_files": deps[offset:offset+limit]}

    if analysis == "functional-analysis":
        result = await asyncio.to_thread(api.analyze_functional_purity)
        return {"target": path.as_posix(), "analysis": analysis, "functional_analysis": result}

    if analysis == "pagerank":
        try:
            with api._pagerank_lock:
                if api._pagerank_cache is None:
                    pr_scores = await asyncio.to_thread(api.graph.pagerank)
                    api._pagerank_cache = {pr.get("node_id"): pr.get("score", 0.0) for pr in pr_scores}
                filtered_pr = [
                    {"node_id": node_id, "score": score}
                    for node_id, score in api._pagerank_cache.items()
                    if node_id and node_id.startswith(path.as_posix())
                ]
            filtered_pr.sort(key=lambda x: x["score"], reverse=True)
            top_pr = filtered_pr[offset:offset+limit]
            return {"target": path.as_posix(), "analysis": analysis, "pagerank": top_pr}
        except Exception as e:
            raise AnalysisError(f"PageRank calculation failed: {e}")

    return {"error": "Unknown analysis"}
