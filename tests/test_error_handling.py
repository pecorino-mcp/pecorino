import json
import pytest
from src.core.errors import (
    PecorinoError,
    SecurityValidationError,
    TargetNotFoundError,
    IndexNotFoundError,
    AnalysisError
)
from src.mcp_server.errors import handle_mcp_error

def _parse_error(res):
    """Parse structured JSON error from CallToolResult."""
    return json.loads(res.content[0].text)

def test_security_validation_error_handling():
    error = SecurityValidationError("Invalid output location")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    payload = _parse_error(res)
    assert payload["error_type"] == "validation"
    assert "Invalid output location" in payload["message"]
    assert "suggestion" in payload

def test_security_validation_with_valid_values():
    error = SecurityValidationError(
        "Invalid view: 'dependents'",
        valid_values=["summary", "classes", "functions"],
        suggestion="Use one of the listed view values.",
    )
    res = handle_mcp_error("browse", error, 0)
    payload = _parse_error(res)
    assert payload["error_type"] == "validation"
    assert payload["valid_values"] == ["summary", "classes", "functions"]
    assert "dependents" in payload["message"]

def test_target_not_found_error_handling():
    error = TargetNotFoundError("Not found: some_file.py")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    payload = _parse_error(res)
    assert payload["error_type"] == "not_found"
    assert "some_file.py" in payload["message"]
    assert "browse" in payload["suggestion"]

def test_index_not_found_error_handling():
    error = IndexNotFoundError("fts index missing")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    payload = _parse_error(res)
    assert payload["error_type"] == "index_missing"
    assert "update_index" in payload["suggestion"]

def test_analysis_error_handling():
    error = AnalysisError("AST parsing failure")
    res = handle_mcp_error("metrics", error, 0)
    assert res.is_error is True
    payload = _parse_error(res)
    assert payload["error_type"] == "analysis"
    assert "AST parsing failure" in payload["message"]

def test_generic_internal_error_handling():
    error = ValueError("unexpected value")
    res = handle_mcp_error("browse", error, 0)
    assert res.is_error is True
    payload = _parse_error(res)
    assert payload["error_type"] == "internal"
    assert "unexpected value" in payload["message"]
