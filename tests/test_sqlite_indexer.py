import os
import pytest
from src.gitstats_index import CodeSearchIndex

def test_sqlite_fts5_indexing(tmp_path):
    db_path = str(tmp_path / "test_search.db")
    index = CodeSearchIndex(db_path)
    
    # Index some nodes
    index.index_node(
        name="calculate_distance",
        node_type="function",
        filepath="src/math.py",
        body_text="def calculate_distance(a, b):\n    return a - b",
        start_line=10,
        end_line=11,
        metrics={"cyclomatic_complexity": 2}
    )
    
    index.index_node(
        name="UserHandler",
        node_type="class",
        filepath="src/auth.py",
        body_text="class UserHandler:\n    def login(self):\n        pass",
        start_line=5,
        end_line=7,
        metrics={"wmc": 5}
    )
    
    # Search for "calculate"
    results = index.search("calculate")
    assert len(results) == 1
    assert results[0]['name'] == "calculate_distance"
    assert results[0]['metrics']['cyclomatic_complexity'] == 2
    
    # Search for "login"
    results = index.search("login")
    assert len(results) == 1
    assert results[0]['name'] == "UserHandler"
    
    # Clear file
    index.clear_file("src/math.py")
    results = index.search("calculate")
    assert len(results) == 0
