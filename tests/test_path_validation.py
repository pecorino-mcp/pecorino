import os
import platform
import pytest
from pathlib import Path
from unittest.mock import patch

from src.mcp_server.config import settings
from src.mcp_server.core import (
    is_safe_path,
    safe_path,
)
from src.core.errors import TargetNotFoundError, SecurityValidationError

def test_is_safe_path_within_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    file_inside = workspace / "test.py"
    file_inside.touch()

    with patch.object(settings, "workspace_root", workspace):
        assert is_safe_path(str(file_inside)) is True

def test_is_safe_path_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    file_outside = outside / "test.py"
    file_outside.touch()

    with patch.object(settings, "workspace_root", workspace):
        assert is_safe_path(str(file_outside)) is False

def test_is_safe_path_traversal_blocked(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    file_outside = outside / "test.py"
    file_outside.touch()

    with patch.object(settings, "workspace_root", workspace):
        traversal = workspace / "../outside/test.py"
        assert is_safe_path(str(traversal)) is False

def test_is_safe_path_allow_external(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    external = tmp_path / "external_repo"
    external.mkdir()
    file_external = external / "test.py"
    file_external.touch()

    with patch.object(settings, "workspace_root", workspace), \
         patch.object(settings, "allowed_external_roots", {external}):
        # Without allow_external, should be blocked
        assert is_safe_path(str(file_external)) is False
        # With allow_external and target under allowed root, should be allowed
        assert is_safe_path(str(file_external), allow_external=True) is True

def test_is_safe_path_allow_external_without_allowlist(tmp_path):
    """allow_external=True with empty allowlist should block everything."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external_repo"
    external.mkdir()
    file_external = external / "test.py"
    file_external.touch()

    with patch.object(settings, "workspace_root", workspace), \
         patch.object(settings, "allowed_external_roots", set()):
        assert is_safe_path(str(file_external), allow_external=True) is False

def test_is_safe_path_allow_external_wrong_root(tmp_path):
    """allow_external=True should block paths not under any allowed root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = tmp_path / "allowed_repo"
    allowed.mkdir()
    other = tmp_path / "other_repo"
    other.mkdir()
    file_other = other / "test.py"
    file_other.touch()

    with patch.object(settings, "workspace_root", workspace), \
         patch.object(settings, "allowed_external_roots", {allowed}):
        assert is_safe_path(str(file_other), allow_external=True) is False

def test_safe_path_function(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch.object(settings, "workspace_root", workspace):
        file_inside = workspace / "test.py"
        file_inside.touch()

        resolved = safe_path(str(file_inside))
        assert resolved == file_inside.resolve()

        with pytest.raises(TargetNotFoundError, match="Not found"):
            safe_path(str(workspace / "non_existent.py"))

        outside_file = tmp_path / "outside.py"
        outside_file.touch()
        with pytest.raises(SecurityValidationError, match="Path outside allowed workspace"):
            safe_path(str(outside_file))


def test_is_safe_path_project_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    other_project = tmp_path / "other_project"
    other_project.mkdir()
    (other_project / ".git").mkdir()
    
    file_in_other = other_project / "src" / "test.py"
    file_in_other.parent.mkdir()
    file_in_other.touch()
    
    with patch.object(settings, "workspace_root", workspace):
        # File is outside workspace_root and no allow_external, but it's inside a project workspace (.git)
        assert is_safe_path(str(file_in_other)) is True


def test_is_safe_path_cwd(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    cwd_dir = tmp_path / "my_cwd"
    cwd_dir.mkdir()
    
    file_in_cwd = cwd_dir / "test.py"
    file_in_cwd.touch()
    
    with patch.object(settings, "workspace_root", workspace):
        with patch("pathlib.Path.cwd", return_value=cwd_dir):
            assert is_safe_path(str(file_in_cwd)) is True



def test_find_repo_root(tmp_path):
    from src.mcp_server.index_db import find_repo_root

    # 1. Create a simulated git repo
    git_repo = tmp_path / "my_git_repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()

    sub_dir = git_repo / "src" / "subdir"
    sub_dir.mkdir(parents=True)
    test_file = sub_dir / "code.py"
    test_file.touch()

    # Root of test_file should be git_repo
    root = find_repo_root(str(test_file))
    assert Path(root).resolve() == git_repo.resolve()

    # 2. Create a normal directory with no .git
    normal_dir = tmp_path / "normal_dir"
    normal_dir.mkdir()
    test_file_2 = normal_dir / "plain.py"
    test_file_2.touch()

    # Root should fallback to containing directory (normal_dir)
    root_2 = find_repo_root(str(test_file_2))
    assert Path(root_2).resolve() == normal_dir.resolve()


def test_ramdisk_index_copy_on_enter(tmp_path):
    from src.mcp_server.ramdisk import RamdiskIndex

    # 1. Create simulated files on SSD path
    ssd_db = tmp_path / "index_code_search.duckdb"
    ssd_db.write_text("dummy database content")

    ssd_graph = tmp_path / "index_gorgonzola"
    ssd_graph.mkdir()
    (ssd_graph / "graph.db").write_text("dummy graph data")

    # 2. Enter ramdisk context
    with RamdiskIndex(str(ssd_db)) as ram:
        # Verify copied over to RAM disk
        ram_db_path = Path(ram.db_path)
        ram_graph_path = Path(ram.gorgonzola_path)

        assert ram_db_path.exists()
        assert ram_db_path.read_text() == "dummy database content"

        assert ram_graph_path.exists()
        assert (ram_graph_path / "graph.db").exists()
        assert (ram_graph_path / "graph.db").read_text() == "dummy graph data"

        # Modify the RAM files
        ram_db_path.write_text("updated database content")
        (ram_graph_path / "graph.db").write_text("updated graph data")

    # 3. Verify changes synced back to SSD on exit
    assert ssd_db.read_text() == "updated database content"
    assert (ssd_graph / "graph.db").read_text() == "updated graph data"
