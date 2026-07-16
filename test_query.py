from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
graph = GorgonzolaGraph('/home/lechibang/.pecorino/indexes/fe40e25a6bb02e2fb085733f99fd70f9_gorgonzola')
res = graph.query('MATCH (n)-[:IMPORTS]->(h) RETURN h.name AS header, count(n) AS frequency ORDER BY frequency DESC LIMIT 10')
print(res)
