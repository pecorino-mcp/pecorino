import asyncio
from src.mcp_server.tools.update_index import do_update_index
from src.mcp_server.context_helper import PecorinoContext

async def main():
    ctx = PecorinoContext(None)
    await do_update_index(target=".", allow_external=True)

asyncio.run(main())
