import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import unquote

import mcp_types as types
from mcp.server import ServerRequestContext

from src.mcp_server.context_helper import PecorinoContext
from src.mcp_server.index_db import get_db_path_for_repo
from src.mcp_server.middleware.caching import _get_cached_api

logger = logging.getLogger(__name__)

async def handle_list_resources(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None = None
) -> types.ListResourcesResult:
    helper = PecorinoContext(ctx)
    roots = []
    try:
        raw_roots = await helper.require_roots()
        for r in (raw_roots or []):
            uri = getattr(r, "uri", None)
            if uri is None and isinstance(r, dict):
                uri = r.get("uri")
            if uri and uri.startswith("file://"):
                roots.append(unquote(uri[7:]))
    except Exception as e:
        logger.warning(f"Could not get roots for resources: {e}")
        # fallback to settings.workspace_root
        from src.mcp_server.config import settings
        if settings.workspace_root:
            roots.append(str(settings.workspace_root))

    resources = []
    for root in set(roots):
        db_path = get_db_path_for_repo(root)
        if Path(db_path).exists():
            repo_hash = hashlib.md5(root.encode('utf-8')).hexdigest()
            # 1. Summary resource
            resources.append(
                types.Resource(
                    uri=f"pecorino://index/{repo_hash}/summary",
                    name=f"Repository Summary ({Path(root).name})",
                    mimeType="application/json",
                    description=f"Language breakdown and counts for {root}"
                )
            )
            # 2. Files list resource
            resources.append(
                types.Resource(
                    uri=f"pecorino://index/{repo_hash}/files",
                    name=f"Indexed Files ({Path(root).name})",
                    mimeType="application/json",
                    description=f"List of all indexed files in {root}"
                )
            )

    return types.ListResourcesResult(resources=resources)


async def handle_read_resource(
    ctx: ServerRequestContext,
    params: types.ReadResourceRequestParams
) -> types.ReadResourceResult:
    uri = params.uri
    if not uri.startswith("pecorino://index/"):
        raise ValueError(f"Unknown resource URI: {uri}")

    parts = uri.replace("pecorino://index/", "").split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid resource URI: {uri}")

    repo_hash = parts[0]
    resource_type = parts[1]

    # We need to find the actual duckdb file based on the hash
    from src.mcp_server.index_db import get_indexes_dir
    db_path = Path(get_indexes_dir()) / f"{repo_hash}_code_search.duckdb"
    if not db_path.exists():
        raise ValueError(f"Index database not found for hash: {repo_hash}")

    conn, _ = _get_cached_api(str(db_path))

    if resource_type == "summary":
        lang_counts = conn.execute("SELECT lang, count(*) as c FROM files GROUP BY lang").fetchall()
        total_files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        total_symbols = conn.execute("SELECT count(*) FROM code_nodes").fetchone()[0]

        data = {
            "total_files": total_files,
            "total_symbols": total_symbols,
            "languages": {row[0]: row[1] for row in lang_counts}
        }
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(data, indent=2))]
        )

    elif resource_type == "files":
        files = conn.execute("SELECT filepath, lang, mtime FROM files ORDER BY filepath").fetchall()
        data = [{"filepath": r[0], "language": r[1], "mtime": r[2]} for r in files]
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(data, indent=2))]
        )

    raise ValueError(f"Unknown resource type: {resource_type}")
