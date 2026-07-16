import pytest
from src.mcp_server.handlers.resources import handle_list_resources, handle_read_resource
from src.mcp_server.registry import registry

from unittest.mock import MagicMock

@pytest.mark.asyncio
async def test_list_resources():
    repo_path = "/tmp/mock_repo_resources"
    registry.register_repo(repo_path, "duckdb_path", "gorgonzola_path")
    
    ctx_mock = MagicMock()
    # Mock require_roots to avoid fetching from real context
    from src.mcp_server.config import settings
    settings.workspace_root = "/tmp/mock_repo_resources"
    
    # Touch the DB file so it passes the exists check
    import hashlib
    from src.mcp_server.index_db import get_db_path_for_repo
    db_path = get_db_path_for_repo(repo_path)
    import os
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    open(db_path, "w").close()
    
    resources = (await handle_list_resources(ctx_mock)).resources
    
    repo = registry.get_repo_by_path(repo_path)
    hash_str = repo["hash"]
    
    # We should have summary, files, and maybe some others
    uris = [r.uri for r in resources]
    assert any(f"/index/{hash_str}/summary" in u for u in uris)
    assert any(f"/index/{hash_str}/files" in u for u in uris)

@pytest.mark.asyncio
async def test_read_resource_invalid():
    ctx_mock = MagicMock()
    params_mock = MagicMock()
    params_mock.uri = "pecorino://index/invalid/summary"
    with pytest.raises(ValueError, match="Index database not found for hash"):
        await handle_read_resource(ctx_mock, params_mock)
