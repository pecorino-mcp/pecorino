from src.mcp_server.registry import registry
print("Repos:", [r['name'] for r in registry.get_all_repos()])
