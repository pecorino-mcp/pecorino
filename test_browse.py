import asyncio, json
import src.mcp_server.core as core

async def main():
    res = await core.do_browse(
        target='/run/media/lechibang/cb09d199-3769-4ec8-9af5-954929515428/projects/gitstats3/src/mcp_server/indexer.py',
        view='classes'
    )
    print(json.dumps(res, indent=2))
asyncio.run(main())
