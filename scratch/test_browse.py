import asyncio
import os
import json
from src.mcp_server.tools.browse import do_browse

async def test():
    # Set up some basic context needed for testing if necessary
    # Assuming standard target within the project
    target = os.path.abspath("src/mcp_server")
    print(f"Testing browse with view='all' on {target}")
    
    result = await do_browse(target=target, view="all")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    asyncio.run(test())
