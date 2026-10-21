from pathlib import Path

from src.mcp_server.config import settings
from src.mcp_server.registry import RegistryDB


def test_registry_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "index_dir", tmp_path)
    registry = RegistryDB()

    repo_path = str(tmp_path / "mock_repo")
    db_path = str(tmp_path / "mock_repo.sqlite3")
    gorgonzola_path = str(tmp_path / "mock_repo_gorgonzola")

    registry.register_repo(repo_path, db_path, gorgonzola_path)

    repos = registry.get_all_repos()
    assert len(repos) == 1

    repo = registry.get_repo_by_path(repo_path)
    assert repo is not None
    assert repo['repo_path'] == str(Path(repo_path).resolve())
    assert repo['db_path'] == db_path
    assert repo['duckdb_path'] == db_path  # Compatibility alias
    assert repo['gorgonzola_path'] == gorgonzola_path
    assert repo['name'] == "mock_repo"

def test_registry_upsert(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "index_dir", tmp_path)
    registry = RegistryDB()
    repo_path = str(tmp_path / "mock_repo_upsert")

    registry.register_repo(repo_path, "old.sqlite3", "old_gorgonzola")
    repo = registry.get_repo_by_path(repo_path)
    assert repo['db_path'] == "old.sqlite3"
    assert repo['duckdb_path'] == "old.sqlite3"

    registry.register_repo(repo_path, "new.sqlite3", "new_gorgonzola")
    repo = registry.get_repo_by_path(repo_path)
    assert repo['db_path'] == "new.sqlite3"
    assert repo['duckdb_path'] == "new.sqlite3"
    assert repo['gorgonzola_path'] == "new_gorgonzola"
