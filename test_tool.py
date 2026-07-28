import asyncio
from src.mcp_server.tools.query_graph import do_query_graph
from mcp.server.context import ServerRequestContext
from unittest.mock import Mock

async def main():
    ctx = Mock(spec=ServerRequestContext)
    query = "MATCH (c:CodeNode) WHERE c.kind = 'Function' AND c.name CONTAINS 'download' RETURN c.name, c.file LIMIT 50"
    res = await do_query_graph(query=query, target="", ctx=ctx)
    print("Result:", res)

asyncio.run(main())
