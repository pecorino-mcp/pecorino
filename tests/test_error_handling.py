import pytest
from src.core.errors import (
    PecorinoError,
    SecurityValidationError,
    TargetNotFoundError,
    IndexNotFoundError,
    AnalysisError
)
from src.mcp_server.errors import handle_mcp_error

def test_security_validation_error_handling():
    error = SecurityValidationError("Invalid output location")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    assert "Security Policy Violation" in res.content[0].text
    assert "Invalid output location" in res.content[0].text

def test_target_not_found_error_handling():
    error = TargetNotFoundError("Not found: some_file.py")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    assert "Target Not Found" in res.content[0].text
    assert "some_file.py" in res.content[0].text

def test_index_not_found_error_handling():
    error = IndexNotFoundError("fts index missing")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    assert "Index Uninitialized" in res.content[0].text
    assert "update_index" in res.content[0].text

def test_analysis_error_handling():
    error = AnalysisError("AST parsing failure")
    res = handle_mcp_error("metrics", error, 0)
    assert res.is_error is True
    assert "Analysis Failed" in res.content[0].text
    assert "AST parsing failure" in res.content[0].text

def test_generic_internal_error_handling():
    error = ValueError("unexpected value")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    assert "Internal Server Error" in res.content[0].text
    assert "unexpected value" in res.content[0].text
