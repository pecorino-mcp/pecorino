"""Tests for the Tantivy BM25F search index."""
import pytest

from src.mcp_server.tantivy_search import TantivyIndex, get_tantivy_path_for_repo


@pytest.fixture
def sample_nodes():
    """Create a list of sample code_node dicts for testing."""
    return [
        {
            "id": "src/main.py::connect_database::10",
            "name": "connect_database",
            "kind": "function",
            "filepath": "src/main.py",
            "hcgs_summary": "Opens a PostgreSQL connection with retry logic",
            "body_text": "def connect_database(host, port, retries=3): conn = psycopg2.connect(host=host, port=port)",
        },
        {
            "id": "src/main.py::DatabaseClient::1",
            "name": "DatabaseClient",
            "kind": "class",
            "filepath": "src/main.py",
            "hcgs_summary": "Client wrapper for database operations",
            "body_text": "class DatabaseClient: def __init__(self): self.conn = None",
        },
        {
            "id": "src/parser.py::parse_query::5",
            "name": "parse_query",
            "kind": "function",
            "filepath": "src/parser.py",
            "hcgs_summary": "Parse a SQL query string into an AST",
            "body_text": "def parse_query(sql): tokens = tokenize(sql) return build_ast(tokens)",
        },
        {
            "id": "src/utils.py::retry::1",
            "name": "retry",
            "kind": "function",
            "filepath": "src/utils.py",
            "hcgs_summary": "Decorator for retrying a function on failure",
            "body_text": "def retry(max_attempts=3): def decorator(func): pass return decorator",
        },
        {
            "id": "tests/test_db.py::test_connect::1",
            "name": "test_connect",
            "kind": "function",
            "filepath": "tests/test_db.py",
            "hcgs_summary": "Test database connection establishment",
            "body_text": "def test_connect(): client = DatabaseClient() client.connect()",
        },
    ]


@pytest.fixture
def tantivy_index(sample_nodes, tmp_path):
    """Build a Tantivy index from sample nodes."""
    idx = TantivyIndex()
    idx.build(sample_nodes, index_path=str(tmp_path / "tantivy_test"))
    return idx


class TestTantivyIndex:
    """Tests for TantivyIndex build, open, and search."""

    def test_build_creates_index(self, sample_nodes, tmp_path):
        """Build should create an index directory and return doc count."""
        idx = TantivyIndex()
        count = idx.build(sample_nodes, index_path=str(tmp_path / "test_idx"))
        assert count == 5
        assert idx.is_ready
        assert (tmp_path / "test_idx").exists()

    def test_build_with_empty_nodes(self, tmp_path):
        """Building with no nodes should create an empty index."""
        idx = TantivyIndex()
        count = idx.build([], index_path=str(tmp_path / "empty_idx"))
        assert count == 0
        assert idx.is_ready

    def test_open_existing_index(self, sample_nodes, tmp_path):
        """Open should load a previously built index."""
        path = str(tmp_path / "persist_idx")
        idx1 = TantivyIndex()
        idx1.build(sample_nodes, index_path=path)
        idx1.close()

        idx2 = TantivyIndex()
        assert idx2.open(index_path=path)
        assert idx2.is_ready

    def test_open_nonexistent_path(self, tmp_path):
        """Open should return False for a nonexistent path."""
        idx = TantivyIndex()
        assert not idx.open(index_path=str(tmp_path / "nonexistent"))
        assert not idx.is_ready

    def test_search_by_name(self, tantivy_index):
        """Searching by function name should rank the matching symbol first."""
        results = tantivy_index.search("connect_database")
        assert len(results) > 0
        top_id, _ = results[0]
        assert "connect_database" in top_id

    def test_search_by_kind(self, tantivy_index):
        """Searching for 'class' should boost the class node."""
        results = tantivy_index.search("class DatabaseClient")
        assert len(results) > 0
        ids = [r[0] for r in results]
        assert "src/main.py::DatabaseClient::1" in ids

    def test_search_by_summary(self, tantivy_index):
        """Searching by summary content should find relevant results."""
        results = tantivy_index.search("PostgreSQL connection retry")
        assert len(results) > 0
        top_id, _ = results[0]
        assert "connect_database" in top_id

    def test_search_returns_scores(self, tantivy_index):
        """Results should include positive BM25 scores."""
        results = tantivy_index.search("database")
        assert len(results) > 0
        for _, score in results:
            assert score > 0.0

    def test_search_limit(self, tantivy_index):
        """Limit parameter should cap result count."""
        results = tantivy_index.search("function", limit=2)
        assert len(results) <= 2

    def test_search_with_custom_boosts(self, tantivy_index):
        """Custom field boosts should change ranking."""
        # With very high name boost, name match should dominate
        boosts_name = {"name": 100.0, "kind": 0.0, "summary": 0.0, "filepath": 0.0, "body": 0.0}
        results_name = tantivy_index.search("parse_query", field_boosts=boosts_name)
        assert len(results_name) > 0
        assert "parse_query" in results_name[0][0]

    def test_search_empty_query(self, tantivy_index):
        """Empty query should return empty results or handle gracefully."""
        results = tantivy_index.search("")
        # tantivy may return empty or raise — both are acceptable
        assert isinstance(results, list)

    def test_search_not_ready(self):
        """Searching before build/open should return empty list."""
        idx = TantivyIndex()
        assert idx.search("test") == []

    def test_close(self, tantivy_index):
        """Close should release resources."""
        tantivy_index.close()
        assert not tantivy_index.is_ready
        assert tantivy_index.search("test") == []

    def test_rebuild_overwrites(self, sample_nodes, tmp_path):
        """Rebuilding at the same path should replace the old index."""
        path = str(tmp_path / "rebuild_idx")
        idx = TantivyIndex()
        idx.build(sample_nodes[:2], index_path=path)
        results1 = idx.search("parse_query")

        # Rebuild with all nodes
        idx.build(sample_nodes, index_path=path)
        results2 = idx.search("parse_query")
        assert len(results2) >= len(results1)


class TestGetTantivyPath:
    """Tests for the path derivation utility."""

    def test_derives_path(self):
        """Should replace _code_search with _tantivy."""
        result = get_tantivy_path_for_repo("/home/user/.pecorino/indexes/abc123_code_search.duckdb")
        assert result.endswith("abc123_tantivy")
        assert "_code_search" not in result

    def test_preserves_directory(self):
        """Should keep the parent directory."""
        result = get_tantivy_path_for_repo("/home/user/.pecorino/indexes/abc123_code_search.duckdb")
        assert result.startswith("/home/user/.pecorino/indexes/")
