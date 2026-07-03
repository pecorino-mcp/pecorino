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
    
    from src.mcp_server.config import settings
    settings.index_dir = test_index_dir
