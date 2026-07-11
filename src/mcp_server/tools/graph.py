import asyncio
import logging
from typing import Optional

from src.core.errors import AnalysisError, IndexNotFoundError, SecurityValidationError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import check_suspicious, safe_path
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
        raise SecurityValidationError(
            f"Invalid analysis type: '{analysis}'",
            valid_values=sorted(ALLOWED_ANALYSES),
            suggestion="Use one of the listed analysis types.",
        )

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
        page = callers[offset:offset+limit]
        result = {"target": symbol, "analysis": analysis, "callers": page}
        # Auto-expand: include source signatures when few results
        if len(page) <= 5 and page:
            index = _get_cached_api(repo_root, db_path, "index")
            for c in page:
                fp = c.get("filepath") or c.get("file")
                if fp:
                    try:
                        file_nodes = await asyncio.to_thread(index.get_file_nodes, fp)
                        cname = c.get("name", "")
                        for n in file_nodes:
                            if n.get("name") == cname:
                                body = n.get("body_text", "")
                                # Include just the first 5 lines (the signature)
                                sig_lines = body.split("\n")[:5] if body else []
                                c["signature_preview"] = "\n".join(sig_lines)
                                break
                    except Exception:
                        pass
            result["auto_expanded"] = True
        if len(callers) > limit:
            result["total_count"] = len(callers)
        return result

    if analysis == "callees":
        callees = await asyncio.to_thread(api.find_callees, symbol)
        page = callees[offset:offset+limit]
        result = {"target": symbol, "analysis": analysis, "callees": page}
        if len(callees) > limit:
            result["total_count"] = len(callees)
        return result

    if analysis == "impact":
        deps = await asyncio.to_thread(api.impact_analysis, path.as_posix(), max_depth)
        page = deps[offset:offset+limit]
        result = {"target": path.as_posix(), "analysis": analysis, "dependent_files": page}
        # Adaptive summary when results exceed page size
        if len(deps) > limit:
            # Group by top-level package directory
            pkg_counts: dict[str, int] = {}
            for d in deps:
                fp = d if isinstance(d, str) else (d.get("filepath", "") if isinstance(d, dict) else str(d))
                parts = fp.split("/")
                # Use first 2 meaningful path segments as package identifier
                pkg = "/".join(parts[:3]) if len(parts) >= 3 else fp
                pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
            top_packages = sorted(pkg_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            result["summary"] = {
                "total_impacted_files": len(deps),
                "top_impacted_packages": [
                    {"package": pkg, "file_count": count}
                    for pkg, count in top_packages
                ],
                "max_depth_reached": max_depth,
                "hint": "Use offset/limit to paginate, or narrow target to a specific subdirectory.",
            }
        return result

    if analysis == "functional-analysis":
        result = await asyncio.to_thread(api.analyze_functional_purity)
        ret = {"target": path.as_posix(), "analysis": analysis, "functional_analysis": result}

        try:
            import json
            import time
            dumped = json.dumps(ret)
            if len(dumped) > 50000:
                output_file = f".mcp_outputs/analyze_{analysis}_{int(time.time())}.json"
                from src.mcp_server.middleware.security import safe_output_path
                out_path = safe_output_path(output_file)
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(dumped)
                return {
                    "target": path.as_posix(),
                    "analysis": analysis,
                    "saved_to": str(out_path),
                    "summary": f"Functional analysis output is too large ({len(dumped)} chars). Results saved to file.",
                    "next_steps": "Review the saved JSON file for detailed functional purity metrics."
                }
        except Exception:
            pass
        return ret

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
            page = filtered_pr[offset:offset+limit]
            result = {"target": path.as_posix(), "analysis": analysis, "pagerank": page}
            if len(filtered_pr) > limit:
                result["summary"] = {
                    "total_ranked_files": len(filtered_pr),
                    "hint": "Use offset/limit to paginate.",
                }
            return result
        except Exception as e:
            raise AnalysisError(f"PageRank calculation failed: {e}")

    return {"error": "Unknown analysis"}
