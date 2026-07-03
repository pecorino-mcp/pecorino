import pytest
from src.mcp_server.federated_graph import FederatedGraphAPI
from src.mcp_server.registry import registry
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
import os
import tempfile
import shutil

@pytest.fixture
def mock_kuzu_repo():
    temp_dir = tempfile.mkdtemp()
    kuzu_path = os.path.join(temp_dir, "test_kuzu")
    # Initialize a blank kuzu graph
    graph = GorgonzolaGraph(kuzu_path)
    with graph:
        pass
        
    repo_path = os.path.join(temp_dir, "mock_repo")
    os.makedirs(repo_path, exist_ok=True)
    registry.register_repo(repo_path, "mock.duckdb", kuzu_path)
    
    yield repo_path
    
    shutil.rmtree(temp_dir, ignore_errors=True)

def test_federated_graph_creation(mock_kuzu_repo):
    # This should trigger the federated graph building logic and not crash
    api = FederatedGraphAPI(mock_kuzu_repo)
    assert api.graph is not None
    assert api.graph.gorgonzola_db_path.endswith("federated_kuzu")
    
    # Check that we can run a simple query on the in-memory graph
    with api.graph:
        res = api.graph._conn.execute("MATCH (a:File) RETURN count(a)").get_all()
        assert len(res) == 1
