import sys
import os
sys.path.append(os.getcwd())
from src.mcp_server.index_pipeline import CodebaseIndexer
from src.mcp_server.config import settings
print("Index dir:", settings.index_dir)
import shutil
shutil.rmtree(settings.index_dir, ignore_errors=True)
indexer = CodebaseIndexer(os.getcwd())
indexer.index_directory(os.getcwd())
