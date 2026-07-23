import pytest
import time
from pathlib import Path
from src.mcp_server.lsp.manager import LSPClientPool

def test_lsp_client_pool_lifecycle(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    file_a = workspace / "module_a.py"
    file_b = workspace / "module_b.py"
    
    file_b.write_text("""
def greet(name: str):
    return f"Hello, {name}"
""", encoding="utf-8")

    file_a.write_text("""
from module_b import greet

def main():
    print(greet("World"))
""", encoding="utf-8")

    pool = LSPClientPool(workspace_root=str(workspace), pool_size=2)
    started = pool.start()
    assert started is True
    assert len(pool.clients) == 2
    
    try:
        time.sleep(1.0)
        pool.open_document(str(file_a), file_a.read_text())
        pool.open_document(str(file_b), file_b.read_text())
        
        # Test batch definition resolution
        batch_res = pool.resolve_definitions_batch(str(file_a), [(5, 10)], timeout_per_query=2.0)
        assert len(batch_res) == 1
        assert batch_res[0]["def_filepath"] == str(file_b)
        assert batch_res[0]["def_line"] == 2
    finally:
        pool.stop()
