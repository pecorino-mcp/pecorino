import pytest
from src.mcp_server.dsl.compiler import DSLCompiler

def test_compiler_nodes_select():
    query_json = {
        "select": "nodes",
        "where": {
            "node_type": {"eq": "function"},
            "filepath": {"like": "%/src/%"}
        },
        "limit": 10
    }
    sql, cypher, params = DSLCompiler.compile(query_json)
    assert "SELECT id, name, node_type, filepath, start_line, end_line FROM main.code_nodes" in sql
    assert "AND node_type = ?" in sql
    assert "AND filepath LIKE ?" in sql
    assert params == ["function", "%/src/%"]
    assert cypher is None

def test_compiler_files_select():
    query_json = {
        "select": "files",
        "where": {
            "lang": "python"
        }
    }
    sql, cypher, params = DSLCompiler.compile(query_json)
    assert "SELECT filepath, lang, mtime FROM main.files" in sql
    assert "AND lang = ?" in sql
    assert params == ["python"]
    assert cypher is None

def test_compiler_invalid_select():
    with pytest.raises(ValueError):
        DSLCompiler.compile({"select": "invalid"})

def test_compiler_with_join_graph():
    query_json = {
        "select": "nodes",
        "join_graph": {
            "relationship": "CALLS",
            "target_name": "MyClass"
        }
    }
    sql, cypher, params = DSLCompiler.compile(query_json)
    assert cypher == "MATCH (n)-[:CALLS]->(target) WHERE target.name = 'MyClass' RETURN n.id AS id"
