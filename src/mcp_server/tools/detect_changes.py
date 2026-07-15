import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import ServerRequestContext

from src.core.errors import AnalysisError
from src.mcp_server.middleware.caching import _get_cached_api
from src.mcp_server.middleware.security import safe_path

logger = logging.getLogger(__name__)

def _get_git_diff_ranges(repo_root: str, diff_target: str = "HEAD") -> Dict[str, List[tuple]]:
    cmd = ["git", "diff", "-U0", diff_target]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AnalysisError(f"Git diff failed: {proc.stderr}")
        
    diff_text = proc.stdout
    changes = {}
    current_file = None
    
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            if current_file not in changes:
                changes[current_file] = []
        elif line.startswith("@@ ") and current_file:
            parts = line.split(" ")
            new_range = parts[2]
            new_range = new_range[1:]
            if "," in new_range:
                start, count = map(int, new_range.split(","))
                if count == 0:
                    end = start
                else:
                    end = start + count - 1
            else:
                start = int(new_range)
                end = start
            changes[current_file].append((start, end))
            
    return changes

async def do_detect_changes(
    target: str,
    diff_target: str = "HEAD",
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Detects changed symbols and their impact using git diff and the AST index."""
    
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    
    path = safe_path(target, allow_external)
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)
    
    changes = await asyncio.to_thread(_get_git_diff_ranges, repo_root, diff_target)
    
    if not changes:
        return {"status": "success", "message": "No changes detected.", "changed_nodes": [], "impact": []}
        
    graph_api = _get_cached_api(repo_root, db_path, "graph")
    index = _get_cached_api(repo_root, db_path, "index")
    if not graph_api or not index:
        raise AnalysisError("Index not found. Run update_index first.")
        
    conn = index._conn
    changed_nodes = []
    
    # Map changed lines to nodes
    for rel_file, ranges in changes.items():
        abs_file = os.path.join(repo_root, rel_file)
        
        conditions = []
        for start, end in ranges:
            conditions.append(f"({start} <= end_line AND {end} >= start_line)")
            
        if not conditions:
            continue
            
        cond_str = " OR ".join(conditions)
        query = f"SELECT id, name, kind FROM code_nodes WHERE filepath = ? AND ({cond_str})"
        
        try:
            rows = conn.execute(query, (abs_file,)).fetchall()
            for r in rows:
                changed_nodes.append({"id": r[0], "name": r[1], "kind": r[2]})
        except Exception as e:
            logger.warning(f"DuckDB query failed for {abs_file}: {e}")
            
    # Deduplicate nodes by name (since DuckDB IDs include line numbers, we want Kuzu IDs which are usually prefixes)
    unique_names = list(set(n["name"] for n in changed_nodes))
    
    impact_results = []
    
    if unique_names:
        # Trace impact for the changed node names
        # Max depth 2 for blast radius to keep it concise
        for name in unique_names:
            try:
                cypher = """
                MATCH p = (source:CodeNode)-[:CALLS|DEPENDS_ON*1..2]->(target:CodeNode)
                WHERE target.name = $name
                RETURN source.name AS caller, source.filepath AS filepath
                LIMIT 10
                """
                res = graph_api.graph.query(cypher, {"name": name})
                if res:
                    impact_results.append({
                        "changed_symbol": name,
                        "impacted_callers": res
                    })
            except Exception as e:
                logger.warning(f"Graph impact query failed for {name}: {e}")
                
    return {
        "status": "success",
        "changed_files_count": len(changes),
        "changed_nodes": changed_nodes,
        "impact_analysis": impact_results
    }
