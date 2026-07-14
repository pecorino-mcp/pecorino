import sys
import kuzu
from src.mcp_server.index_db import get_db_path_for_repo, get_graph_path_for_repo

duck_path = get_db_path_for_repo("/media/lechibang/work/projects/mycelium")
graph_path = get_graph_path_for_repo(duck_path)
print(f"Kuzu Path: {graph_path}")

db = kuzu.Database(graph_path)
conn = kuzu.Connection(db)
res = conn.execute("MATCH (n) RETURN count(n)")
while res.has_next():
    print("Node count:", res.get_next())
