import pytest
import time
from pathlib import Path
from src.mcp_server.index_pipeline import CodebaseIndexer

def test_lsp_end_to_end_resolution(tmp_path):
    # Create test workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    file_b = workspace / "module_b.py"
    file_b.write_text("""
def target_function(x):
    return x * 2
""", encoding="utf-8")

    file_a = workspace / "module_a.py"
    file_a.write_text("""
from module_b import target_function

def caller_function():
    # Call target_function on line 5
    val = target_function(10)
    return val
""", encoding="utf-8")

    # Initialize Indexer
    indexer = CodebaseIndexer(repo_path=str(workspace))
    indexer.enable_lsp = True
    
    try:
        # Run Indexing
        res = indexer.index_directory(str(workspace))
        assert res["status"] == "success"
        
        # Verify direct CALLS edge in Kuzu DB: caller_function -> target_function
        with indexer.graph as g:
            q = (
                "MATCH (c:Function)-[:CALLS]->(t:Function) "
                "RETURN c.name AS caller, t.name AS callee"
            )
            rows = g.query(q)
            
            assert len(rows) >= 1
            call_map = {r["caller"]: r["callee"] for r in rows}
            assert "caller_function" in call_map
            assert call_map["caller_function"] == "target_function"
            
            # Verify that we did not just build a call pointing to a Symbol node
            to_sym = g.query("MATCH (:Function)-[:CALLS]->(s:Symbol) RETURN s.name AS name")
            # Symbol name can still exist in symbol table but CALLS should point to Function node
            # Let's verify that the main CALLS edge goes to a Function node
            target_node = g.query("MATCH (c:Function)-[:CALLS]->(t) WHERE t.name = 'target_function' RETURN label(t) AS lbl")
            assert len(target_node) >= 1
            assert target_node[0]["lbl"] == "Function"
    finally:
        indexer.close()
