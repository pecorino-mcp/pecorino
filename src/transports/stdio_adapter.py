import io
import logging
import sys

import anyio

logger = logging.getLogger(__name__)

async def run_stdio(mcp_server):
    import mcp.server.stdio

    # Capture the original stdout buffer before redirecting sys.stdout
    original_stdout_buffer = sys.stdout.buffer
    mcp_stdout = anyio.wrap_file(io.TextIOWrapper(original_stdout_buffer, encoding="utf-8"))

    # Redirect sys.stdout to sys.stderr to prevent application logger.info() calls from corrupting stdio transport
    sys.stdout = sys.stderr

    async with mcp.server.stdio.stdio_server(stdout=mcp_stdout) as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options()
        )
