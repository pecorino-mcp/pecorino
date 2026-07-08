import asyncio
from typing import Optional
from mcp.server import ServerRequestContext

from src.mcp_server.tools.update_index import do_update_index
from src.mcp_server.tools.metrics_tool import do_metrics

async def do_risk_triage(target: str, allow_external: bool = False, ctx: Optional[ServerRequestContext] = None) -> dict:
    """Run a basic risk triage on a repository.
    
    This updates the index to ensure freshness, then calculates hotspots.
    """
    # 1. Update index
    try:
        await do_update_index(target=target, allow_external=allow_external, ctx=ctx)
    except Exception as e:
        return {"error": f"Failed to update index during risk triage: {e}"}
        
    # 2. Get hotspots metrics
    try:
        metrics_res = await do_metrics(target=target, what=["hotspots"], allow_external=allow_external)
        
        summary = {
            "target": target,
            "status": "success",
            "message": "Risk triage completed.",
        }
        if "hotspots" in metrics_res:
            hotspots = metrics_res["hotspots"]
            summary["riskiest_files"] = hotspots[:10]  # Return top 10 riskiest files
            summary["next_steps"] = "To drill into a specific file's issues, call metrics on that file with what=['complexity', 'oop']."
        elif "report_path" in metrics_res:
            summary["report_path"] = metrics_res["report_path"]
            summary["next_steps"] = "A full report was saved. Check the report file for detailed hotspots."
        else:
            summary["metrics_raw"] = metrics_res
            summary["next_steps"] = "To drill into a specific file's issues, call metrics on that file with what=['complexity', 'oop']."
            
        return summary
    except Exception as e:
        return {"error": f"Failed to compute metrics during risk triage: {e}"}
