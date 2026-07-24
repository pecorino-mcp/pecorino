import pytest
import os
import asyncio
from unittest.mock import patch, MagicMock
from src.mcp_server.tools.search import do_search

@pytest.mark.asyncio
async def test_search_explainability():
    """Test that setting explain=True returns ranking explanation features."""
    repo_root = os.getcwd()
    
    mock_results = [{
        "id": "dummy_node_1",
        "filepath": "dummy.py",
        "name": "explainable",
        "kind": "Function",
        "score": 0.95,
        "ranking_explanation": {
            "fts_score": 0.8,
            "vector_sim": 0.9,
            "pagerank": 0.1
        }
    }]
    
    # We will just patch `index.search` in `_do_fts` directly.
    with patch('src.mcp_server.index_db.get_db_path_for_repo', return_value="dummy.db"), \
         patch('src.mcp_server.tools.search._auto_sync_stale', return_value=None), \
         patch('src.mcp_server.tools.search._get_cached_api') as mock_get_api:
         
        mock_index = MagicMock()
        # Ensure search is an async mock if it's awaited, wait, index.search is sync!
        mock_index.search.return_value = mock_results
        mock_index.has_fts_index.return_value = True
        mock_index.is_fts_dirty.return_value = False
        
        def side_effect(root, db, name):
            if name == "index": return mock_index
            if name == "graph": return MagicMock()
            return MagicMock()
            
        mock_get_api.side_effect = side_effect
        
        # Search without explain
        res_no_explain = await do_search(
            target=repo_root,
            query="explainable",
            mode="fts",
            limit=5,
            explain=False,
            include_source=False
        )
        assert res_no_explain["search_status"] == "ok"
        results = res_no_explain.get("groups", [])[0].get("files", [])
        assert len(results) > 0
        
        # We need to verify that index.search was called with explain=False
        mock_index.search.assert_called_with('explainable', 5, repo_root, 0, mode='fts', boost_ids=None, explain=False)
        
        # Search with explain
        res_explain = await do_search(
            target=repo_root,
            query="explainable",
            mode="fts",
            limit=5,
            explain=True,
            include_source=False
        )
        assert res_explain["search_status"] == "ok"
        results = res_explain.get("groups", [])[0].get("files", [])
        assert len(results) > 0
        
        # We need to verify that index.search was called with explain=True
        mock_index.search.assert_called_with('explainable', 5, repo_root, 0, mode='fts', boost_ids=None, explain=True)
        
        explanation = results[0].get("ranking_explanation")
        assert explanation is not None, "ranking_explanation should be present when explain=True"
        assert isinstance(explanation, dict)
        assert "fts_score" in explanation
        assert "vector_sim" in explanation
        assert "pagerank" in explanation
