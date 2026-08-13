import asyncio
import os
import sys

# Ensure we can import from src
sys.path.insert(0, os.path.abspath('.'))

from src.mcp_server.tools.update_index import do_update_index

async def main():
    try:
        res = await do_update_index(target=".", allow_external=True, ctx=None)
        print("Success:", res)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
