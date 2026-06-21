import sys
import asyncio
import argparse
from pathlib import Path

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))


from src.mcp_server.config import settings
from src.mcp_server.core import server as mcp_server

def main():
    parser = argparse.ArgumentParser(description="Run OOP Metrics Analyzer Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], help="Transport protocol (overrides MCP_TRANSPORT env var)")
    parser.add_argument("--host", help="Host address for network transports (overrides HOST env var)")
    parser.add_argument("--port", type=int, help="Port number for network transports (overrides PORT env var)")

    args, unknown = parser.parse_known_args()

    # CLI args override environment variables if provided
    if args.transport:
        settings.transport = args.transport
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    # Run global safe startup migration
    from src.mcp_server.index import migrate_all
    migrate_all()

    if settings.transport == "stdio":
        from src.transports.stdio_adapter import run_stdio
        asyncio.run(run_stdio(mcp_server))
    elif settings.transport == "sse":
        try:
            import uvicorn
            import fastapi
        except ImportError:
            print("Error: fastapi and uvicorn must be installed to use SSE transport.", file=sys.stderr)
            sys.exit(1)
        from src.transports.fastapi_adapter import run_sse
        asyncio.run(run_sse(mcp_server))
    elif settings.transport == "streamable-http":
        try:
            import uvicorn
            import fastapi
        except ImportError:
            print("Error: fastapi and uvicorn must be installed to use streamable-http transport.", file=sys.stderr)
            sys.exit(1)
        from src.transports.fastapi_adapter import run_streamable_http
        asyncio.run(run_streamable_http(mcp_server))

if __name__ == "__main__":
    main()
