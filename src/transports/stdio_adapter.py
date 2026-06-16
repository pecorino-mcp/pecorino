import io
import sys
import anyio
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel import NotificationOptions

async def run_stdio(mcp_server):
    import mcp.server.stdio
    
    # Capture the original stdout buffer before redirecting sys.stdout
    original_stdout_buffer = sys.stdout.buffer
    mcp_stdout = anyio.wrap_file(io.TextIOWrapper(original_stdout_buffer, encoding="utf-8"))
    
    # Redirect sys.stdout to sys.stderr to prevent application print() calls from corrupting stdio transport
    sys.stdout = sys.stderr

    async with mcp.server.stdio.stdio_server(stdout=mcp_stdout) as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="gitstats3",
                server_version="3.0.0",
                capabilities=mcp_server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            )
        )
