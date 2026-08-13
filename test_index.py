import asyncio
from src.mcp_server.tools.update_index import do_update_index

async def main():
    try:
        print("Running update_index...")
        res = await do_update_index(target=".")
        print(res)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
