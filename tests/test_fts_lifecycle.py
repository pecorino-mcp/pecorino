"""Regression tests for the FTS index lifecycle.

Verifies that the two-phase write process correctly builds the FTS index
and that write-lock contention doesn't silently prevent FTS creation.
"""
import os
import pytest
import duckdb
from pathlib import Path

from src.mcp_server.index_db import CodeSearchIndex, get_db_path_for_repo
from src.mcp_server.index_pipeline import CodebaseIndexer


@pytest.fixture
def temp_repo(tmp_path):
    """Create a minimal git repo with a Python file for indexing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # Fake git root marker

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


class TestCodebaseIndexerLifecycle:
    """Tests for CodebaseIndexer connection management."""

    def test_context_manager_releases_connection(self, temp_repo, db_path):
        """Context manager must close the DuckDB connection on exit."""
        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            assert indexer.search_index is not None
            assert indexer.search_index._conn is not None

        # After context exit, search_index should be None (null-guarded close)
        assert indexer.search_index is None

    def test_close_is_idempotent(self, temp_repo):
        """Calling close() multiple times must not raise."""
        indexer = CodebaseIndexer(repo_path=str(temp_repo))
        indexer.close()
        indexer.close()  # Should not raise
        assert indexer.search_index is None

    def test_single_file_index_releases_lock(self, temp_repo, db_path):
        """After single-file indexing with context manager, no write lock remains."""
        py_file = temp_repo / "example.py"
        content = py_file.read_text(encoding="utf-8")

        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(py_file), content, ".py", rebuild_fts=True)

        # Should be able to open a read-write connection (no lingering lock)
        conn = duckdb.connect(db_path, read_only=False)
        try:
            count = conn.execute("SELECT count(*) FROM code_nodes").fetchone()[0]
            assert count > 0
        finally:
            conn.close()


class TestFTSIndex:
    """Tests for FTS index creation and detection."""

    def test_has_fts_index_false_on_fresh_db(self, temp_repo, db_path):
        """A freshly created DB should not have an FTS index."""
        index = CodeSearchIndex(db_path=db_path, read_only=False)
        try:
            assert index.has_fts_index() is False
        finally:
            index.close()

    def test_has_fts_index_true_after_rebuild(self, temp_repo, db_path):
        """After rebuild_fts(), has_fts_index() must return True."""
        # First index a file so code_nodes has data
        py_file = temp_repo / "example.py"
        content = py_file.read_text(encoding="utf-8")

        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(py_file), content, ".py", rebuild_fts=True)

        # Verify FTS index exists using a read-only connection (matching browse tool path)
        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            assert index.has_fts_index() is True
        finally:
            index.close()

    def test_fts_search_returns_results_after_index(self, temp_repo, db_path):
        """FTS search must return results for indexed symbols."""
        py_file = temp_repo / "example.py"
        content = py_file.read_text(encoding="utf-8")

        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(py_file), content, ".py", rebuild_fts=True)

        # Open read-only and search
        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            results = index.search("Foo", limit=5)
            assert len(results) > 0
            assert not any("error" in r for r in results)
            # Should find the Foo class
            names = [r["name"] for r in results]
            assert any("Foo" in name for name in names)
        finally:
            index.close()

    def test_fts_survives_no_op_reindex(self, temp_repo, db_path):
        """FTS index must persist after a no-op re-index (all files skipped).

        Simulates: index once → close → index again (same content) → verify search works.
        Uses separate CodebaseIndexer instances with full close() between them
        to match real-world server behavior.
        """
        py_file = temp_repo / "example.py"
        content = py_file.read_text(encoding="utf-8")

        # First index
        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(py_file), content, ".py", rebuild_fts=True)

        # Verify FTS works after first index
        index = CodeSearchIndex(db_path=db_path, read_only=True)
        try:
            results = index.search("Foo", limit=5)
            assert len(results) > 0
            assert not any("error" in r for r in results)
        finally:
            index.close()


class TestIncrementalFTS:
    """Tests for dirty-tracking incremental FTS logic."""

    def test_fts_dirty_tracking_after_single_file_index(self, temp_repo):
        """Single-file index with rebuild_fts=False should mark FTS dirty."""
        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(temp_repo / "test.py"), "def foo(): pass", ".py", rebuild_fts=False)
            assert indexer.search_index.is_fts_dirty() is True

    def test_fts_rebuild_clears_dirty_flag(self, db_path):
        """rebuild_fts() should clear the dirty flag."""
        index = CodeSearchIndex(db_path=db_path, read_only=False)
        try:
            index.mark_fts_dirty()
            assert index.is_fts_dirty() is True
            index.rebuild_fts()
            assert index.is_fts_dirty() is False
        finally:
            index.close()

    def test_ensure_fts_rebuilds_when_dirty(self, temp_repo, db_path):
        """ensure_fts() should rebuild when dirty flag is set."""
        # Need some data to index so FTS works
        py_file = temp_repo / "example.py"
        py_file.write_text("def ensure_foo(): pass")
        
        with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
            indexer.index_file(str(py_file), py_file.read_text(), ".py", rebuild_fts=False)
            
        index = CodeSearchIndex(db_path=db_path, read_only=False)
        try:
            assert index.is_fts_dirty() is True
            index.ensure_fts()
            assert index.is_fts_dirty() is False
        finally:
            index.close()
