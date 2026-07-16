import pytest
from pathlib import Path
from src.mcp_server.registry import RegistryDB

def test_registry_registration():
    registry = RegistryDB()
    
    repo_path = "/tmp/mock_repo"
    duckdb_path = "/tmp/mock_repo.duckdb"
    gorgonzola_path = "/tmp/mock_repo_gorgonzola"
    
    registry.register_repo(repo_path, duckdb_path, gorgonzola_path)
    
    repos = registry.get_all_repos()
    assert len(repos) >= 1
    
    repo = registry.get_repo_by_path(repo_path)
    assert repo is not None
    assert repo['repo_path'] == str(Path(repo_path).resolve())
    assert repo['duckdb_path'] == duckdb_path
    assert repo['gorgonzola_path'] == gorgonzola_path
    assert repo['name'] == "mock_repo"
    
def test_registry_upsert():
    registry = RegistryDB()
    repo_path = "/tmp/mock_repo_upsert"
    
    registry.register_repo(repo_path, "old.duckdb", "old_gorgonzola")
    repo = registry.get_repo_by_path(repo_path)
    assert repo['duckdb_path'] == "old.duckdb"
    
    registry.register_repo(repo_path, "new.duckdb", "new_gorgonzola")
    repo = registry.get_repo_by_path(repo_path)
    assert repo['duckdb_path'] == "new.duckdb"
    assert repo['gorgonzola_path'] == "new_gorgonzola"
