import logging
from pathlib import Path

from src.mcp_server.config import settings

logger = logging.getLogger(__name__)

async def do_set_workspace(path_arg: str, helper=None) -> list[dict]:
    """Change the server's workspace root directory at runtime."""
    if not path_arg:
        raise ValueError("Missing 'path' argument")

    new_path = Path(path_arg).expanduser().resolve()
    if not new_path.is_dir():
        raise ValueError(f"Path does not exist or is not a directory: {new_path}")

    # Update settings
    settings.workspace_root = new_path

    # Restart file watcher with new path if it's running
    from src.mcp_server.middleware.file_watcher import get_file_watcher
    watcher = get_file_watcher()
    if watcher:
        watcher.stop()
        watcher.start(new_path)

    # Clear index cache to force re-indexing of the new workspace
    from src.mcp_server.index_db import clear_index_cache
    clear_index_cache()

    # Notify clients that roots and resources changed
    if helper:
        await helper.notify_roots_list_changed()
        await helper.notify_resource_list_changed()

    return [{"type": "text", "text": f"Workspace root successfully changed to: {new_path}"}]
