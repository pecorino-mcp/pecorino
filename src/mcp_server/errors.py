import json
import logging
import sys
import time
import traceback
import mcp_types as types
from src.core.errors import (
    PecorinoError,
    SecurityValidationError,
    TargetNotFoundError,
    IndexNotFoundError,
    AnalysisError
)
from src.mcp_server.prometheus_metrics import TOOL_ERRORS, TOOL_DURATION

logger = logging.getLogger(__name__)

def handle_mcp_error(tool_name: str, error: Exception, start_time: float) -> types.CallToolResult:
    """
    Unified error handler that maps codebase and standard exceptions 
    to structured, self-correcting JSON payloads that help LLMs recover.
    """
    duration = time.time() - start_time
    TOOL_DURATION.labels(tool=tool_name).observe(duration)

    logger.error("MCP Tool Failure: '%s' after %.4fs - Error: %s", tool_name, duration, error)
    # Only print full stack trace for unexpected internal errors (non-PecorinoError exceptions)
    if not isinstance(error, PecorinoError):
        traceback.print_exc(file=sys.stderr)

    # Build a structured error response that LLMs can reason about
    error_payload: dict = {}
    error_type = "internal"

    if isinstance(error, SecurityValidationError):
        error_type = "security"
        error_payload["error_type"] = "validation"
        error_payload["message"] = str(error)
        if getattr(error, 'valid_values', None):
            error_payload["valid_values"] = error.valid_values
        if getattr(error, 'suggestion', None):
            error_payload["suggestion"] = error.suggestion
        elif getattr(error, 'valid_values', None):
            error_payload["suggestion"] = "Use one of the listed valid_values."
        else:
            error_payload["suggestion"] = "Check the parameter value and try again."
    elif isinstance(error, TargetNotFoundError):
        error_type = "not_found"
        error_payload["error_type"] = "not_found"
        error_payload["message"] = str(error)
        error_payload["suggestion"] = "Verify the path exists. Use the 'browse' tool with view='tree' to list available files."
    elif isinstance(error, IndexNotFoundError):
        error_type = "index_missing"
        error_payload["error_type"] = "index_missing"
        error_payload["message"] = str(error)
        error_payload["suggestion"] = "Run the 'update_index' tool on this target first to build the search index and graph."
    elif isinstance(error, AnalysisError):
        error_type = "analysis"
        error_payload["error_type"] = "analysis"
        error_payload["message"] = str(error)
        error_payload["suggestion"] = "Try narrowing the target scope, reducing max_depth, or running 'update_index' to refresh the index."
    elif isinstance(error, PecorinoError):
        error_type = "pecorino_internal"
        error_payload["error_type"] = "pecorino_error"
        error_payload["message"] = str(error)
    else:
        error_type = "internal"
        error_payload["error_type"] = "internal"
        error_payload["message"] = f"Internal Server Error: {str(error)}"
        error_payload["suggestion"] = "This is an unexpected error. Try again or report the issue."

    TOOL_ERRORS.labels(tool=tool_name, error_type=error_type).inc()

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(error_payload, indent=2))],
        is_error=True
    )
