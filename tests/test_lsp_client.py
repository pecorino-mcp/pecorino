import pytest
import time
from pathlib import Path
from src.mcp_server.lsp.manager import LSPClient

def test_lsp_client_lifecycle(tmp_path):
    # Create test workspace
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

    # Start LSP Client
    client = LSPClient(workspace_root=str(workspace))
    started = client.start()
    assert started is True
    
    try:
        # Give pylsp a moment to index the workspace
        time.sleep(1.0)
        
        # Open documents in LSP
        client.open_document(str(file_a), file_a.read_text())
        client.open_document(str(file_b), file_b.read_text())
        
        # In file_a.py, "greet" on line 5 (print(greet("World"))) is at index 10 (print(gr...)
        # Line 5 (1-indexed), column 10 (0-indexed). Let's query definition.
        # Wait, let's verify line numbers:
        # line 1: blank
        # line 2: from module_b import greet
        # line 3: blank
        # line 4: def main():
        # line 5:     print(greet("World"))
        # In main(), 'greet' starts at column 10 (4 spaces + 5 for print + 1 for open-paren = 10)
        defn = client.resolve_definition(str(file_a), line=5, character=10)
        assert defn is not None
        assert defn["filepath"] == str(file_b)
        assert defn["start_line"] == 2 # def greet starts on line 2 in file_b
    finally:
        client.stop()
