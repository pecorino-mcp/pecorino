import os
import shutil
import pytest
import asyncio
from pathlib import Path

from src.mcp_server.tools.update_index import do_update_index
from src.mcp_server.index_db import CodeSearchIndex
from src.core.errors import IndexNotFoundError

@pytest.fixture
def temp_workspace(tmp_path):
    # Setup a dummy python project
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    
    # Create a dummy python file
    dummy_file = repo_dir / "dummy.py"
    dummy_file.write_text("def hello():\n    print('world')\n")
    
    # Setup .git to mimic a project root
    (repo_dir / ".git").mkdir()
    
    yield repo_dir

@pytest.mark.asyncio
async def test_update_index_single_file(temp_workspace):
    """Test that indexing a single file works and does not crash with UnboundLocalError."""
    target_file = temp_workspace / "dummy.py"
    
    res = await do_update_index(str(target_file))
    
    assert res["status"] == "success"
    assert res["indexed_files"] == 1
    assert "summary" in res

def test_code_search_index_read_only_missing_db(temp_workspace):
    """Test that instantiating CodeSearchIndex in read-only mode on a non-existent DB raises IndexNotFoundError, instead of creating a blank DB."""
    missing_db = temp_workspace / "missing.sqlite3"
    assert not missing_db.exists()
    
    with pytest.raises(IndexNotFoundError) as exc:
        idx = CodeSearchIndex(db_path=str(missing_db), read_only=True)
    
    assert "Index database not found" in str(exc.value)
    # Ensure the file was NOT created
    assert not missing_db.exists()
