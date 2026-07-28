import os
from src.mcp_server.index_db import get_db_path_for_repo
from src.mcp_server.config import settings

print("Workspace root:", settings.workspace_root)
print("DB Path:", get_db_path_for_repo(str(settings.workspace_root)))
