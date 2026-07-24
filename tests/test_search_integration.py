import pytest
import asyncio
from src.mcp_server.tools.search import do_search

@pytest.mark.asyncio
async def test_do_search_includes_context(monkeypatch, tmp_path):
    # We want to test that do_search correctly returns context fields
    # when include_context=True
    
    # 1. Mock the find_repo_root and get_db_path_for_repo
    import src.mcp_server.index_db as index_db_mod
    monkeypatch.setattr(index_db_mod, "find_repo_root", lambda x: str(tmp_path))
    monkeypatch.setattr(index_db_mod, "get_db_path_for_repo", lambda x: str(tmp_path / "test.db"))
    
    # Mock auto_sync_stale
    # Mock safe_path
    import src.mcp_server.tools.search as search_mod
    monkeypatch.setattr(search_mod, "safe_path", lambda p, *args, **kwargs: tmp_path)
    # Mock auto_sync_stale
    async def mock_auto_sync(*args, **kwargs):
        pass
    monkeypatch.setattr(search_mod, "_auto_sync_stale", mock_auto_sync)
    
    # Mock the index API caching
    import src.mcp_server.middleware.caching as cache_mod
    class MockIndexAPI:
        def has_fts_index(self): return True
        def is_fts_dirty(self): return False
        def search(self, *args, **kwargs):
            return [{"id": "1", "name": "foo", "filepath": "a.py", "kind": "function"}]
            
    class MockGraphGraph:
        def query(self, cypher, params=None):
            if "CALLS" in cypher:
                return [{"id": "2", "name": "caller", "filepath": "b.py", "line": 10}]
            return []
            
    class MockGraphAPI:
        def __init__(self):
            self.graph = MockGraphGraph()
            
    def mock_get_cached_api(repo, db, kind):
        if kind == "index":
            return MockIndexAPI()
        elif kind == "graph":
            return MockGraphAPI()
            
    monkeypatch.setattr(search_mod, "_get_cached_api", mock_get_cached_api)
    
    # Call do_search with include_context=True
    res = await do_search(
        target=str(tmp_path),
        query="foo",
        mode="fts",
        include_context=True
    )
    
    assert "groups" in res
    assert len(res["groups"]) > 0
    
    files = res["groups"][0]["files"]
    assert len(files) == 1
    
    # Check that context was assembled
    assert "callers" in files[0]
    assert files[0]["callers"][0]["name"] == "caller"
