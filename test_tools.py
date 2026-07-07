import asyncio
import json
from src.mcp_server.tools.search import do_search
from src.mcp_server.tools.query import do_query
from src.mcp_server.tools.graph import do_analyze
from src.mcp_server.tools.browse import do_browse

async def main():
    target = "/media/lechibang/work/projects/pecorino"
    
    print("=== Testing Auto-Expanding Context (search) ===")
    # Query something specific that returns < 3 results
    res1 = await do_search(target, query="DROP_PROJECTED_GRAPH", limit=10, include_source=False)
    print(f"Results count: {len(res1['results'])}")
    print(f"Auto-expanded: {res1.get('auto_expanded')}")
    if res1['results'] and res1.get('auto_expanded'):
        body = res1['results'][0].get('body_text', '')
        print(f"Body included: {bool(body)} (length: {len(body)})")
    print()
    
    print("=== Testing Intent-Based Presets (query_codebase) ===")
    from src.mcp_server.tools.query import INTENT_PRESETS
    query_json = INTENT_PRESETS["dead_code"]
    res2 = await do_query(target, query_json=query_json)
    print(f"Intent applied: {res2.get('intent')}")
    print(f"Results count: {len(res2.get('results', []))}")
    print()
    
    print("=== Testing Adaptive Summarization (graph impact) ===")
    res3 = await do_analyze(target, analysis="impact", max_depth=3)
    if "summary" in res3:
        print("Summary envelope included:")
        print(json.dumps(res3["summary"], indent=2))
    else:
        print(f"No summary, items: {len(res3.get('dependent_files', []))}")
    print()
    
    print("=== Testing Adaptive Summarization (browse summary) ===")
    res4 = await do_browse(target, view="summary")
    struct = res4.get("structure", {})
    if "architectural_groups" in struct:
        print("Architectural Groups included:")
        for k, v in struct["architectural_groups"].items():
            print(f"  {k}: {v['file_count']} files")
    else:
        print("No architectural groups found")
        
asyncio.run(main())
