import asyncio
from typing import Optional
from mcp.server import ServerRequestContext

from src.mcp_server.tools.search import do_search
from src.mcp_server.tools.graph import do_analyze

async def do_explain_symbol(target: str, query: str, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    """Explain a specific symbol by searching for it and finding its callers."""
    try:
        search_res = await do_search(
            target=target, 
            query=query, 
            limit=5, 
            include_source=True, 
            allow_external=allow_external, 
            ctx=ctx
        )
    except Exception as e:
        search_res = {"error": f"Search failed: {e}"}
        
    try:
        callers_res = await do_analyze(
            target=target,
            analysis="callers",
            symbol=query,
            limit=5,
            allow_external=allow_external,
            ctx=ctx
        )
    except Exception as e:
        callers_res = {"error": f"Callers analysis failed: {e}"}
        
    return {
        "symbol": query,
        "target": target,
        "search_results": search_res.get("results", search_res),
        "callers": callers_res.get("callers", callers_res),
        "next_steps": "If you need more depth, use analyze(analysis='impact') or browse(view='deps')."
    }
