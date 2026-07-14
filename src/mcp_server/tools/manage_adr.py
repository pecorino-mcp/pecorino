import asyncio
import glob
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server import ServerRequestContext

from src.core.errors import SecurityValidationError
from src.mcp_server.middleware.security import safe_path

logger = logging.getLogger(__name__)

async def do_manage_adr(
    action: str,
    target: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    adr_id: Optional[str] = None,
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """CRUD operations for Architecture Decision Records."""
    
    from src.mcp_server.index_db import find_repo_root
    
    path = safe_path(target, allow_external)
    repo_root = find_repo_root(str(path))
    adr_dir = os.path.join(repo_root, "docs", "adr")
    os.makedirs(adr_dir, exist_ok=True)
    
    if action == "list":
        adrs = []
        for file in glob.glob(os.path.join(adr_dir, "*.md")):
            with open(file, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                adrs.append({
                    "id": os.path.basename(file),
                    "title": first_line.lstrip('# ') if first_line.startswith('#') else "Untitled"
                })
        return {"status": "success", "adrs": sorted(adrs, key=lambda x: x["id"])}
        
    elif action == "create":
        if not title:
            return {"status": "error", "message": "title is required for create"}
        # Find next id
        existing = glob.glob(os.path.join(adr_dir, "*.md"))
        next_id = 1
        if existing:
            ids = []
            for f in existing:
                try:
                    ids.append(int(os.path.basename(f).split("-")[0]))
                except ValueError:
                    pass
            if ids:
                next_id = max(ids) + 1
                
        safe_title = "".join(c if c.isalnum() else "-" for c in title).strip("-").lower()
        filename = f"{next_id:04d}-{safe_title}.md"
        filepath = os.path.join(adr_dir, filename)
        
        date_str = datetime.now().strftime("%Y-%m-%d")
        file_content = f"# {title}\n\nDate: {date_str}\n\n## Status\n\nProposed\n\n## Context\n\n{content or ''}\n\n## Decision\n\n\n## Consequences\n\n"
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(file_content)
            
        try:
            from src.mcp_server.tools.update_index import do_update_index
            await do_update_index(target=filepath, allow_external=True, ctx=ctx)
        except Exception as e:
            logger.warning(f"Failed to auto-index new ADR {filename}: {e}")
            
        return {"status": "success", "message": f"Created {filename}", "id": filename}
        
    elif action == "read":
        if not adr_id:
            return {"status": "error", "message": "adr_id is required for read"}
        filepath = os.path.join(adr_dir, adr_id)
        if not os.path.exists(filepath):
            return {"status": "error", "message": "ADR not found"}
        with open(filepath, 'r', encoding='utf-8') as f:
            return {"status": "success", "content": f.read()}
            
    elif action == "update":
        if not adr_id:
            return {"status": "error", "message": "adr_id is required for update"}
        filepath = os.path.join(adr_dir, adr_id)
        if not os.path.exists(filepath):
            return {"status": "error", "message": "ADR not found"}
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content or "")
            
        try:
            from src.mcp_server.tools.update_index import do_update_index
            await do_update_index(target=filepath, allow_external=True, ctx=ctx)
        except Exception as e:
            logger.warning(f"Failed to auto-index updated ADR {adr_id}: {e}")
            
        return {"status": "success", "message": f"Updated {adr_id}"}
        
    elif action == "delete":
        if not adr_id:
            return {"status": "error", "message": "adr_id is required for delete"}
        filepath = os.path.join(adr_dir, adr_id)
        if not os.path.exists(filepath):
            return {"status": "error", "message": "ADR not found"}
        os.remove(filepath)
        return {"status": "success", "message": f"Deleted {adr_id}"}
        
    else:
        return {"status": "error", "message": f"Unknown action: {action}"}
