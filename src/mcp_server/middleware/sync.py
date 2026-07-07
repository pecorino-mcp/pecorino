import asyncio
import hashlib
import logging
import os
import threading
from pathlib import Path

from src.mcp_server.middleware.caching import clear_index_cache

logger = logging.getLogger(__name__)
_auto_sync_lock = threading.Lock()

async def _auto_sync_stale(repo_root: str, db_path: str, scope_path: str):
    """Detect and re-index files whose on-disk mtime is newer than indexed mtime.

    Runs inline before browse queries to ensure the index reflects current disk state.
    Protected by a lock to prevent concurrent reindexing of the same files.
    """
    from src.mcp_server.index_db import CodeSearchIndex

    def _sync():
        with _auto_sync_lock:
            # If the proactive background watcher is running, it handles stale files 
            # as they change. No need to block queries with an inline check.
            from src.mcp_server.middleware.file_watcher import get_file_watcher
            if get_file_watcher() is not None:
                return 0

            check_index = CodeSearchIndex(db_path=db_path, read_only=True)
            try:
                stale_files = check_index.get_stale_files(scope_path)
            finally:
                check_index.close()

            if not stale_files:
                return 0

            logger.info("Auto-sync: %d stale file(s) detected, re-indexing...", len(stale_files))

            from src.mcp_server.index_pipeline import CodebaseIndexer

            # Must close cached read-only connections before opening a write connection —
            # DuckDB doesn't allow mixing read_only and read_write to the same file.
            clear_index_cache()

            indexer = CodebaseIndexer(repo_path=repo_root)
            try:
                for filepath in stale_files:
                    try:
                        content = Path(filepath).read_text(encoding='utf-8', errors='ignore')
                        ext = os.path.splitext(filepath)[1]
                        indexer.index_file(filepath, content, ext, rebuild_fts=False)
                        mtime = os.path.getmtime(filepath)
                        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                        lang = ext.lstrip('.')
                        indexer.search_index.upsert_file_hash(filepath, content_hash, mtime, lang)
                    except Exception as e:
                        logger.warning("Auto-sync failed for %s: %s", filepath, e)
            finally:
                indexer.close()

            return len(stale_files)

    synced = await asyncio.to_thread(_sync)
    if synced:
        # Clear cached read-only DuckDB connections so they pick up the new data.
        # Preserves GraphAPI (and its PageRank cache) — only invalidates pagerank scores.
        clear_index_cache()
