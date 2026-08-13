import os
import pytest
from pathlib import Path

@pytest.fixture(autouse=True, scope="session")
def isolate_index_dir(tmp_path_factory):
    """
    Force all tests to use a temporary directory for their indexes.
    This prevents leaking hundreds of DuckDB databases into ~/.pecorino/indexes/
    and causing excessive SSD I/O wear during test runs.
    """
    test_index_dir = tmp_path_factory.mktemp("pecorino_test_indexes")
    
    # Set the environment variable so subprocesses also get the isolated directory
    os.environ["PECORINO_INDEX_DIR"] = str(test_index_dir)
    
    # Limit resource usage for embeddings/models/DBs during tests
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"
    os.environ["RAY_num_cpus"] = "1"
    
    from src.mcp_server.config import settings
    settings.index_dir = test_index_dir

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
    from src.mcp_server.index_db import get_db_path_for_repo
    return get_db_path_for_repo(str(temp_repo))

@pytest.fixture
def indexed_repo(temp_repo, db_path):
    """Index the temp repo and return (repo_path, db_path)."""
    import hashlib
    from src.mcp_server.index_pipeline import CodebaseIndexer
    
    py_file = temp_repo / "example.py"
    content = py_file.read_text()
    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
    mtime = os.path.getmtime(str(py_file))

    with CodebaseIndexer(repo_path=str(temp_repo)) as indexer:
        indexer.index_file(str(py_file), content, ".py", rebuild_fts=False)
        indexer.search_index.upsert_file_hash(str(py_file), content_hash, mtime, "py")

    return temp_repo, db_path
