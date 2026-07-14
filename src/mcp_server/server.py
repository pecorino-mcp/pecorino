import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from src.mcp_server.config import settings
from src.mcp_server.core import server as mcp_server

logger = logging.getLogger(__name__)

def _setup_signal_handlers():
    """Setup signal handlers for multi-session stability."""
    def sigint_handler(signum, frame):
        logger.warning("Received SIGINT - ignoring for multi-session stability")

    def sigterm_handler(signum, frame):
        logger.info("Received SIGTERM - shutting down gracefully")

        # Stop file watcher if running
        from src.mcp_server.middleware.file_watcher import get_file_watcher
        watcher = get_file_watcher()
        if watcher:
            watcher.stop()

        sys.exit(0)

    if hasattr(signal, 'SIGINT'):
        signal.signal(signal.SIGINT, sigint_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, sigterm_handler)

_setup_signal_handlers()

def main():
    parser = argparse.ArgumentParser(description="Run OOP Metrics Analyzer Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], help="Transport protocol (overrides MCP_TRANSPORT env var)")
    parser.add_argument("--host", help="Host address for network transports (overrides HOST env var)")
    parser.add_argument("--port", type=int, help="Port number for network transports (overrides PORT env var)")
    parser.add_argument("--embedding-model", help="Embedding model ID (overrides PECORINO_EMBEDDING_MODEL env var)")
    parser.add_argument("--embedding-dim", type=int, help="Embedding dimension (overrides PECORINO_EMBEDDING_DIM env var)")

    parser.add_argument("--legacy", action="store_true", help="Use legacy low-level Server API with JSON routing instead of MCPServer")

    args, _ = parser.parse_known_args()

    # CLI args override environment variables if provided
    if args.transport:
        settings.transport = args.transport
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.embedding_model:
        settings.embedding_model = args.embedding_model
        # Automatically update default dimension if not explicitly provided
        if not args.embedding_dim:
            if "nomic" in args.embedding_model.lower():
                settings.embedding_dim = 768
            elif "bge-large" in args.embedding_model.lower():
                settings.embedding_dim = 1024
            elif "bge-base" in args.embedding_model.lower():
                settings.embedding_dim = 768
            else:
                settings.embedding_dim = 384
    if args.embedding_dim:
        settings.embedding_dim = args.embedding_dim

    # Start the background file watcher on the current workspace root
    if settings.transport == "stdio":
        import sys
        sys.stdout = sys.stderr

    try:
        from src.mcp_server.middleware.file_watcher import init_file_watcher
        watcher = init_file_watcher(settings.workspace_root)
        watcher.start()
    except Exception as e:
        logger.warning(f"Failed to start file watcher: {e}")

    if not args.legacy:
        from src.mcp_server.highlevel_core import server as fast_mcp_server
        kwargs = {}
        if settings.transport in ("sse", "streamable-http"):
            if settings.host: kwargs["host"] = settings.host
            if settings.port: kwargs["port"] = settings.port
        fast_mcp_server.run(transport=settings.transport, **kwargs)
        return

    from src.mcp_server.core import server as mcp_server
    if settings.transport == "stdio":
        from src.transports.stdio_adapter import run_stdio
        asyncio.run(run_stdio(mcp_server))
    elif settings.transport == "sse":
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            logger.warning("Error: fastapi and uvicorn must be installed to use SSE transport.")
            sys.exit(1)
        from src.transports.fastapi_adapter import run_sse
        asyncio.run(run_sse(mcp_server))
    elif settings.transport == "streamable-http":
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            logger.warning("Error: fastapi and uvicorn must be installed to use streamable-http transport.")
            sys.exit(1)
        from src.transports.fastapi_adapter import run_streamable_http
        asyncio.run(run_streamable_http(mcp_server))

if __name__ == "__main__":
    main()
