import pytest
from src.mcp_server.federated_graph import FederatedGraphAPI
from src.mcp_server.registry import registry
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
import os
import tempfile
import shutil

@pytest.fixture
def mock_gorgonzola_repo():
    temp_dir = tempfile.mkdtemp()
    gorgonzola_path = os.path.join(temp_dir, "test_gorgonzola")
    # Initialize a blank gorgonzola graph
    graph = GorgonzolaGraph(gorgonzola_path)
    with graph:
        pass
        
    repo_path = os.path.join(temp_dir, "mock_repo")
    os.makedirs(repo_path, exist_ok=True)
    registry.register_repo(repo_path, "mock.duckdb", gorgonzola_path)
    
    yield repo_path
    
    shutil.rmtree(temp_dir, ignore_errors=True)

def test_federated_graph_creation(mock_gorgonzola_repo):
    # This should trigger the federated graph building logic and not crash
    api = FederatedGraphAPI(mock_gorgonzola_repo)
    assert api.graph is not None
    assert api.graph.gorgonzola_db_path.endswith("federated_gorgonzola")
    
    # Check that we can run a simple query on the in-memory graph
    with api.graph:
        res = api.graph.query("MATCH (a:File) RETURN count(a)")
        assert len(res) == 1
