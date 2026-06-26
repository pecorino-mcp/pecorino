import sys
sys.path.append('src')
from mcp_server.index import CodeSearchIndex

idx = CodeSearchIndex(read_only=True)
pr = idx.graph.pagerank()
print(f"Total nodes in PageRank: {len(pr)}")

# Print top 10
pr.sort(key=lambda x: x['score'], reverse=True)
print("Top 10:")
for x in pr[:10]:
    print(f"{x['score']:.4f}: {x['node_id']}")
