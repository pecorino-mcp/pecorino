"""Tests for Git features extraction and DuckDB bulk persistence (Phase 2)."""
import os
import subprocess

import pytest

from src.mcp_server.index_db import CodeSearchIndex


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary git repository with commit history."""
    repo = tmp_path / "git_repo"
    repo.mkdir()

    # Init git repo
    subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)

    # File 1
    f1 = repo / "main.py"
    f1.write_text("def main(): pass\n")
    subprocess.run(["git", "add", "main.py"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo), check=True)

    # File 2 & bug fix commit
    f2 = repo / "utils.py"
    f2.write_text("def helper(): return 42\n")
    subprocess.run(["git", "add", "utils.py"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "fix: resolve bug in helper"], cwd=str(repo), check=True)

    return str(repo)


class TestGitFeatures:
    """Unit tests for Phase 2 Git Features computation & persistence."""

    def test_compute_git_features(self, temp_git_repo):
        """Verify git log parsing and feature computation."""
        from src.mcp_server.index_pipeline import CodebaseIndexer

        indexer = CodebaseIndexer(repo_path=temp_git_repo)
        features = indexer._compute_git_features(temp_git_repo)

        assert len(features) >= 2

        feat_map = {f["filepath"]: f for f in features}

        main_path = os.path.abspath(os.path.join(temp_git_repo, "main.py"))
        utils_path = os.path.abspath(os.path.join(temp_git_repo, "utils.py"))

        assert main_path in feat_map
        assert utils_path in feat_map

        main_f = feat_map[main_path]
        assert main_f["git_commit_count"] >= 1
        assert main_f["git_authors"] == 1
        assert main_f["git_churn"] > 0
        assert main_f["git_days_since_change"] >= 0

        utils_f = feat_map[utils_path]
        assert utils_f["git_commit_count"] >= 1
        assert utils_f["git_bug_fix_ratio"] > 0.0

    def test_update_git_features_bulk(self, tmp_path):
        """Verify bulk update of git feature columns in DuckDB code_nodes."""
        db_path = str(tmp_path / "test_git.duckdb")
        search_index = CodeSearchIndex(db_path=db_path)

        # Insert sample code_nodes
        main_path = str(tmp_path / "main.py")
        search_index.index_nodes([
            {
                "id": f"{main_path}::main::1",
                "name": "main",
                "kind": "function",
                "filepath": main_path,
                "start_line": 1,
                "end_line": 5,
            }
        ])

        # Apply git features bulk update
        git_feats = [
            {
                "filepath": main_path,
                "git_survival_days": 10,
                "git_rename_count": 1,
                "git_ownership_entropy": 0.95,
                "git_commit_count": 5,
                "git_days_since_change": 2,
                "git_churn": 120,
                "git_authors": 2,
                "git_bug_fix_ratio": 0.40,
            }
        ]
        search_index.update_git_features_bulk(git_feats)

        # Retrieve and verify from DuckDB
        row = search_index._conn.execute("""
            SELECT git_survival_days, git_rename_count, git_ownership_entropy,
                   git_commit_count, git_days_since_change, git_churn,
                   git_authors, git_bug_fix_ratio
            FROM code_nodes
            WHERE filepath = ?
        """, (main_path,)).fetchone()

        assert row is not None
        assert row[0] == 10
        assert row[1] == 1
        assert abs(row[2] - 0.95) < 0.001
        assert row[3] == 5
        assert row[4] == 2
        assert row[5] == 120
        assert row[6] == 2
        assert abs(row[7] - 0.40) < 0.001

        search_index.close()
