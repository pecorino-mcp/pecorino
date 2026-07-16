import asyncio
from src.mcp_server.tools.query_graph import do_query_graph

async def main():
    try:
        res = await do_query_graph(
            "/run/media/lechibang/cb09d199-3769-4ec8-9af5-954929515428/projects/gorgonzola",
            "MATCH (n)-[:IMPORTS]->(h) RETURN h.name AS header, count(n) AS frequency ORDER BY frequency DESC LIMIT 10",
            None
        )
        print("Success:")
        print(res)
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
