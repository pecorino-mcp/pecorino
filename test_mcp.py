import asyncio
import json
import subprocess
import sys

async def main():
    proc = await asyncio.create_subprocess_exec(
        ".venv/bin/python", "-m", "src.mcp_server.server",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr
    )
    
    # Send a search query that returns < 3 results
    req1 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {
                "target": "/media/lechibang/work/projects/pecorino/src/mcp_server/gorgonzola_graph.py",
                "query": "DROP_PROJECTED_GRAPH",
                "include_source": False
            }
        }
    }
    
    # Send an intent-based query that uses entry_points
    req2 = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "query_codebase",
            "arguments": {
                "intent": "entry_points"
            }
        }
    }
    
    proc.stdin.write(json.dumps(req1).encode() + b"\n")
    proc.stdin.write(json.dumps(req2).encode() + b"\n")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "exit"}).encode() + b"\n")
    await proc.stdin.drain()
    
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(line.decode().strip())

asyncio.run(main())
