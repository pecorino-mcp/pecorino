import logging
import os
import re
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)

def to_gorgonzola_id(r: Dict[str, Any]) -> str:
    """Convert an index result dict to its gorgonzola graph ID."""
    filepath = r.get('filepath')
    name = r.get('name')
    nt = r.get('kind', '').lower()
    if not filepath or not name:
        return r.get('id', '')
    if nt == 'method' and '.' in name:
        parts = name.split('.', 1)
        return f"{filepath}::{parts[0]}::{parts[1]}"
    return f"{filepath}::{name}"

def assemble_context(result: Dict[str, Any], graph, workspace_root: str) -> Dict[str, Any]:
    """Enrich a search result with structural and temporal context."""
    context = dict(result)

    node_id = to_gorgonzola_id(result)
    if not node_id:
        return context

    if graph is not None:
        try:
            # 1. Structural queries via Gorgonzola
            # Callers
            callers = graph.query('''
                MATCH (caller)-[:CALLS]->(n:CodeNode {id: $id})
                RETURN caller.id as id, caller.name as name, caller.file as file, caller.line as line
                LIMIT 5
            ''', {"id": node_id})
            if callers:
                context["callers"] = [{"name": c["name"], "file": c.get("file") or c.get("filepath", ""), "line": c.get("line", 0)} for c in callers]

            # Callees
            callees = graph.query('''
                MATCH (n:CodeNode {id: $id})-[:CALLS]->(callee)
                RETURN callee.id as id, callee.name as name, callee.file as file, callee.line as line
                LIMIT 5
            ''', {"id": node_id})
            if callees:
                context["callees"] = [{"name": c["name"], "file": c.get("file") or c.get("filepath", ""), "line": c.get("line", 0)} for c in callees]

            # Parent scope (e.g. class containing this method)
            parent_q = graph.query('''
                MATCH (parent)-[:DEFINES]->(n:CodeNode {id: $id})
                RETURN parent.name as name, parent.kind as kind
                LIMIT 1
            ''', {"id": node_id})
            if parent_q:
                context["parent"] = {"name": parent_q[0]["name"], "kind": parent_q[0]["kind"]}
        except Exception as e:
            logger.warning(f"Failed to assemble graph context for {node_id}: {e}")

    # 2. Temporal queries via Git
    filepath = result.get("filepath")
    if filepath and workspace_root:
        abs_path = os.path.join(workspace_root, filepath)
        if os.path.exists(abs_path):
            try:
                git_log = subprocess.check_output(
                    ["git", "log", "-n", "3", "--format=%H|%s|%an|%ad", "--date=short", "--", abs_path],
                    cwd=workspace_root,
                    stderr=subprocess.DEVNULL
                ).decode("utf-8").strip()

                commits = []
                issues = set()

                if git_log:
                    for line in git_log.split("\n"):
                        if not line:
                            continue
                        parts = line.split("|", 3)
                        if len(parts) == 4:
                            commit_hash, msg, author, date = parts
                            commits.append({"hash": commit_hash[:7], "message": msg, "author": author, "date": date})

                            # Extract issues (e.g., #123, GH-456, PROJ-12)
                            found = re.findall(r'#(\d+)|GH-(\d+)|([A-Z]+-\d+)', msg)
                            for match in found:
                                iss = next((m for m in match if m), None)
                                if iss:
                                    # re-add the # or GH- prefix if it was captured in groups
                                    if match[0]:
                                        iss = f"#{iss}"
                                    elif match[1]:
                                        iss = f"GH-{iss}"
                                    issues.add(iss)

                if commits:
                    context["recent_commits"] = commits
                if issues:
                    context["related_issues"] = list(issues)
            except Exception as e:
                logger.debug(f"Failed to extract git context for {abs_path}: {e}")
                pass

    return context
