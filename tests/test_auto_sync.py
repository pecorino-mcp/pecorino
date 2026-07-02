"""Tests for auto-sync stale file detection and re-indexing.

Verifies that get_stale_files() detects files modified on disk after indexing,
and that _auto_sync_stale() re-indexes them transparently.
"""
import hashlib
import os
import time
import pytest

from src.mcp_server.index_db import CodeSearchIndex, get_db_path_for_repo
from src.mcp_server.index_pipeline import CodebaseIndexer


@pytest.fixture
def temp_repo(tmp_path):
    """Create a minimal git repo with a Python file for indexing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    py_file = repo / "example.py"
    py_file.write_text(
        'class Foo:\n'
        '    def bar(self):\n'
        '        return 42\n'
        '\n'
        'def baz():\n'
        '    return Foo().bar()\n',
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def db_path(temp_repo):
    return get_db_path_for_repo(str(temp_repo))


@pytest.fixture
def indexed_repo(temp_repo, db_path):
    """Index the temp repo and return (repo_path, db_path)."""
    py_file = temp_repo / "example.py"
    content = py_file.read_text()
    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
    mtime = os.path.getmtime(str(py_file))

    with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
        indexer.index_file(str(py_file), content, ".py", rebuild_fts=False)
        indexer.search_index.upsert_file_hash(str(py_file), content_hash, mtime, "py")

    return temp_repo, db_path


class TestGetStaleFiles:
    """Tests for CodeSearchIndex.get_stale_files()."""

    def test_no_stale_files_after_fresh_index(self, indexed_repo):
        """Immediately after indexing, no files should be stale."""
        repo, db_path = indexed_repo
        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files(str(repo))
            assert stale == []
        finally:
            index.close()

    def test_detects_modified_file(self, indexed_repo):
        """Modifying a file on disk should make it appear in get_stale_files()."""
        repo, db_path = indexed_repo
        py_file = repo / "example.py"

        # Ensure mtime advances (some filesystems have 1s resolution)
        time.sleep(0.05)
        py_file.write_text(
            'class Foo:\n'
            '    def renamed_method(self):\n'
            '        return 99\n',
            encoding="utf-8",
        )

        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files(str(repo))
            assert str(py_file) in stale
        finally:
            index.close()

    def test_unscoped_returns_all_stale(self, indexed_repo):
        """get_stale_files() without dirpath should check all tracked files."""
        repo, db_path = indexed_repo
        py_file = repo / "example.py"

        time.sleep(0.05)
        py_file.write_text('def changed(): pass\n', encoding="utf-8")

        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files()  # no dirpath
            assert str(py_file) in stale
        finally:
            index.close()

    def test_scoped_to_different_dir_returns_empty(self, indexed_repo):
        """Scoping to a directory that doesn't contain the file returns empty."""
        repo, db_path = indexed_repo
        py_file = repo / "example.py"

        time.sleep(0.05)
        py_file.write_text('def changed(): pass\n', encoding="utf-8")

        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files("/nonexistent/path")
            assert stale == []
        finally:
            index.close()

    def test_deleted_file_not_in_stale(self, indexed_repo):
        """Deleted files should not appear in stale list (OSError is caught)."""
        repo, db_path = indexed_repo
        py_file = repo / "example.py"
        py_file.unlink()

        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files(str(repo))
            assert stale == []
        finally:
            index.close()


class TestAutoSyncIntegration:
    """Integration test: modify file → browse search finds new symbol."""

    @pytest.mark.asyncio
    async def test_auto_sync_reindexes_modified_file(self, indexed_repo):
        """After modifying a file, _auto_sync_stale should re-index it."""
        repo, db_path = indexed_repo
        py_file = repo / "example.py"

        # Modify the file — add a new function
        time.sleep(0.05)
        py_file.write_text(
            'class Foo:\n'
            '    def bar(self):\n'
            '        return 42\n'
            '\n'
            'def completely_new_function():\n'
            '    return "hello"\n',
            encoding="utf-8",
        )

        from src.mcp_server.core import _auto_sync_stale, clear_api_cache
        await _auto_sync_stale(str(repo), db_path, str(repo))

        # Verify the new function is now in the index
        index = CodeSearchIndex(db_path=db_path, read_only=False)
        try:
            index.rebuild_fts()
            results = index.search("completely_new_function", limit=5)
            assert len(results) > 0
            assert any("completely_new_function" in r.get("name", "") for r in results)
        finally:
            index.close()

    @pytest.mark.asyncio
    async def test_auto_sync_skips_when_nothing_stale(self, indexed_repo):
        """_auto_sync_stale should be a no-op when nothing has changed."""
        repo, db_path = indexed_repo

        from src.mcp_server.core import _auto_sync_stale
        # Should not raise or modify anything
        await _auto_sync_stale(str(repo), db_path, str(repo))

        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            stale = index.get_stale_files(str(repo))
            assert stale == []
        finally:
            index.close()
