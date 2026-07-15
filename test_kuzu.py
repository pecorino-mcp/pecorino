from src.mcp_server.index_db import get_graph_path_for_repo
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

path = get_graph_path_for_repo('main')
graph = GorgonzolaGraph(path)

res = graph.query("MATCH (i:Identifier) RETURN i.raw, i.canonical_verb, i.canonical_entity LIMIT 10")
print("Cypher result:", res)
