import sys
import os
import json
from pathlib import Path

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from src.mcp_server.indexer import CodebaseIndexer

def progress_callback(current: int, total: int, file_path: str):
    print(json.dumps({
        "current": current,
        "total": total,
        "file": file_path
    }), flush=True)

def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: python -m src.mcp_server.index_worker <repo_root> <target_path>\n")
        sys.exit(1)
        
    repo_root = sys.argv[1]
    target_path = sys.argv[2]
    
    try:
        indexer = CodebaseIndexer(repo_path=repo_root)
        res = indexer.index_directory(target_path, progress_callback=progress_callback)
        print(json.dumps({"result": res}), flush=True)
    except Exception as e:
        sys.stderr.write(f"Error during indexing subprocess: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
