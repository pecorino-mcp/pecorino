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
    to standardized MCP CallToolResult payloads.
    """
    duration = time.time() - start_time
    TOOL_DURATION.labels(tool=tool_name).observe(duration)
    TOOL_ERRORS.labels(tool=tool_name).inc()

    logger.error("MCP Tool Failure: '%s' after %.4fs - Error: %s", tool_name, duration, error)
    # Only print full stack trace for unexpected internal errors (non-PecorinoError exceptions)
    if not isinstance(error, PecorinoError):
        traceback.print_exc(file=sys.stderr)

    if isinstance(error, SecurityValidationError):
        msg = f"Security Policy Violation: {str(error)}"
    elif isinstance(error, TargetNotFoundError):
        msg = f"Target Not Found: {str(error)}"
    elif isinstance(error, IndexNotFoundError):
        msg = f"Index Uninitialized: {str(error)}. Please run the 'update_index' tool on this workspace/repository target first to build FTS search and graph dependencies."
    elif isinstance(error, AnalysisError):
        msg = f"Analysis Failed: {str(error)}"
    elif isinstance(error, PecorinoError):
        msg = f"Pecorino Error: {str(error)}"
    else:
        msg = f"Internal Server Error: {str(error)}"

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=msg)],
        is_error=True
    )
