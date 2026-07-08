import asyncio
import os
from src.mcp_server.registry import registry
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

async def main():
    repos = registry.get_all_repos()
    for repo in repos:
        if "po-online" in repo['name']:
            print("Found po-online:", repo)
            graph_path = repo['kuzu_path']
            graph = GorgonzolaGraph(graph_path)
            with graph:
                res1 = graph._conn.execute("MATCH (n:File) WHERE n.id = '/media/lechibang/work/projects/po-online/assets/pokemon-showdown/data/random-battles/randomroulette/teams.ts' RETURN n")
                print("In File table:", res1.has_next())
                res1.close()
                
                res2 = graph._conn.execute("MATCH (a:File)-[r:CONTAINS]->(b:Class) WHERE a.id = '/media/lechibang/work/projects/po-online/assets/pokemon-showdown/data/random-battles/randomroulette/teams.ts' RETURN a.id, b.id")
                print("Edges in CONTAINS:")
                while res2.has_next():
                    print(res2.get_next())
                res2.close()

if __name__ == "__main__":
    asyncio.run(main())
