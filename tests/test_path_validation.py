import os
import platform
import pytest
from pathlib import Path
from unittest.mock import patch

from src.mcp_server.config import settings
from src.mcp_server.core import (
    is_absolute_path,
    has_valid_extension,
    is_safe_path,
    safe_path,
    _ALLOWED_WORKSPACE_ROOTS,
    register_allowed_root,
    unregister_allowed_root
)

def test_is_absolute_path():
    sys_name = platform.system()
    if sys_name == "Windows":
        assert is_absolute_path("C:\\Users\\test") is True
        assert is_absolute_path("\\\\server\\share\\path") is True
        assert is_absolute_path("relative\\path") is False
    else:
        assert is_absolute_path("/home/user/test") is True
        assert is_absolute_path("relative/path") is False
        assert is_absolute_path("./relative/path") is False

def test_has_valid_extension():
    assert has_valid_extension("test.py") is True
    assert has_valid_extension("test.js") is True
    assert has_valid_extension("test.java") is True
    assert has_valid_extension("test.txt") is False
    assert has_valid_extension("test.unknown") is False

def test_is_safe_path_and_workspace_roots(tmp_path):
    allowed_root = tmp_path / "workspace"
    allowed_root.mkdir()
    
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    
    register_allowed_root(allowed_root)
    try:
        file_inside = allowed_root / "test.py"
        file_inside.touch()
        assert is_safe_path(str(file_inside)) is True
        
        file_outside = outside_root / "test.py"
        file_outside.touch()
        assert is_safe_path(str(file_outside)) is False
        
        traversal = allowed_root / "../outside/test.py"
        assert is_safe_path(str(traversal)) is False
        
    finally:
        unregister_allowed_root(allowed_root)

def test_safe_path_function(tmp_path):
    allowed_root = tmp_path / "workspace"
    allowed_root.mkdir()
    register_allowed_root(allowed_root)
    
    try:
        file_inside = allowed_root / "test.py"
        file_inside.touch()
        
        resolved = safe_path(str(file_inside))
        assert resolved == file_inside.resolve()
        
        with pytest.raises(ValueError, match="Not found"):
            safe_path(str(allowed_root / "non_existent.py"))
            
        outside_file = tmp_path / "outside.py"
        outside_file.touch()
        with pytest.raises(ValueError, match="Path outside allowed workspace"):
            safe_path(str(outside_file))
            
    finally:
        unregister_allowed_root(allowed_root)

def test_config_external_dirs(tmp_path):
    test_dir = tmp_path / "external_repo"
    test_dir.mkdir()
    
    temp_config_file = tmp_path / "config.json"
    
    with patch.object(settings, "_config_file", temp_config_file), \
         patch.object(settings, "_config_dir", tmp_path):
        
        settings.allowed_external_dirs = set()
        
        added = settings.add_external_dir(str(test_dir))
        assert Path(added).resolve() == test_dir.resolve()
        assert test_dir.resolve() in settings.allowed_external_dirs
        
        dirs = settings.list_external_dirs()
        assert len(dirs) == 1
        assert dirs[0] == str(test_dir.resolve())
        
        removed = settings.remove_external_dir(str(test_dir))
        assert Path(removed).resolve() == test_dir.resolve()
        assert test_dir.resolve() not in settings.allowed_external_dirs
        assert len(settings.list_external_dirs()) == 0


def test_find_repo_root(tmp_path):
    from src.mcp_server.index import find_repo_root
    
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
