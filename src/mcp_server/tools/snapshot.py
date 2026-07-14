import logging
import os
import subprocess
from typing import Optional

from mcp.server import ServerRequestContext

from src.core.errors import AnalysisError
from src.mcp_server.middleware.caching import clear_index_cache
from src.mcp_server.middleware.security import safe_path

logger = logging.getLogger(__name__)

async def do_manage_snapshot(
    action: str,
    target: str,
    output_path: Optional[str] = None,
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Export or import a .zst graph snapshot."""
    
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo, get_graph_path_for_repo
    
    path = safe_path(target, allow_external)
    repo_root = find_repo_root(str(path))
    
    db_path = get_db_path_for_repo(repo_root)
    graph_path = get_graph_path_for_repo(db_path)
    
    indexes_dir = os.path.dirname(db_path)
    db_name = os.path.basename(db_path)
    graph_name = os.path.basename(graph_path)
    
    if not output_path:
        repo_name = os.path.basename(repo_root)
        output_path = os.path.join(repo_root, f"{repo_name}_snapshot.tar.zst")
        
    if action == "export":
        # Ensure we don't have open connections
        clear_index_cache()
        
        if not os.path.exists(db_path):
            return {"status": "error", "message": "Index does not exist. Run update_index first."}
            
        cmd = ["tar", "--zstd", "-cf", output_path, "-C", indexes_dir, db_name, graph_name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"status": "error", "message": f"Export failed: {proc.stderr}"}
            
        return {"status": "success", "message": f"Snapshot exported to {output_path}"}
        
    elif action == "import":
        if not os.path.exists(output_path):
            return {"status": "error", "message": f"Snapshot file not found: {output_path}"}
            
        # Ensure we don't have open connections
        clear_index_cache()
        
        cmd = ["tar", "--zstd", "-xf", output_path, "-C", indexes_dir]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"status": "error", "message": f"Import failed: {proc.stderr}"}
            
        return {"status": "success", "message": f"Snapshot imported successfully from {output_path}"}
        
    else:
        return {"status": "error", "message": f"Unknown action: {action}"}
