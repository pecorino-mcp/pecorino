from prometheus_client import Counter, Gauge, Histogram

# Prometheus Metrics
TOOL_CALLS = Counter('mcp_tool_calls_total', 'Total number of MCP tool calls', ['tool'])
TOOL_ERRORS = Counter('mcp_tool_errors_total', 'Total number of MCP tool errors', ['tool'])
TOOL_DURATION = Histogram('mcp_tool_duration_seconds', 'Duration of MCP tool execution', ['tool'])
ACTIVE_SESSIONS = Gauge('mcp_active_sessions', 'Current active SSE sessions')
