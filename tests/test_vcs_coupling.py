import pytest
from unittest.mock import MagicMock, patch
from src.mcp_server.index_pipeline import CodebaseIndexer

def test_vcs_coupling_calculation():
    # Mock subprocess.Popen for git log
    # Simulate a log where file_a.py and file_b.py changed together in 3 commits,
    # and file_c.py changed alone in 1 commit.
    mock_stdout = """commit:1111
file_a.py
file_b.py

commit:2222
file_a.py
file_b.py

commit:3333
file_a.py
file_b.py

commit:4444
file_c.py
"""
    # Create mock process
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (mock_stdout, "")
    mock_proc.returncode = 0
    
    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("os.path.exists", return_value=True):
         
        indexer = CodebaseIndexer(repo_path="/fake/repo")
        indexer.enable_lsp = False
        
        edges = indexer._compute_git_coupling("/fake/repo")
        indexer.close()
        
        # Verify co-change weights
        # file_a.py and file_b.py changed in 3/3 commits -> Jaccard similarity = 3 / (3 + 3 - 3) = 1.0
        assert len(edges) == 1
        f1, f2, weight = edges[0]
        assert "file_a.py" in f1 or "file_a.py" in f2
        assert "file_b.py" in f1 or "file_b.py" in f2
        assert weight == 1.0
