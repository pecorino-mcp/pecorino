from mcp.server.models import InitializationOptions
from mcp.server.lowlevel import NotificationOptions

async def run_stdio(mcp_server):
    import mcp.server.stdio
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
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
