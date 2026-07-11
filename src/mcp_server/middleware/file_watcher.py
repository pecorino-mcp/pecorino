import asyncio
import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.mcp_server.events import bus
from src.mcp_server.index_db import get_db_path_for_repo
from src.mcp_server.middleware.caching import clear_index_cache

logger = logging.getLogger(__name__)

class DebouncedFileEventHandler(FileSystemEventHandler):
    def __init__(self, watcher_service, debounce_seconds: float = 2.0):
        super().__init__()
        self.watcher_service = watcher_service
        self.debounce_seconds = debounce_seconds
        self.changed_files = set()
        self._timer = None
        self._lock = threading.Lock()

    def _trigger_sync(self):
        with self._lock:
            files_to_sync = list(self.changed_files)
            self.changed_files.clear()
            self._timer = None

        if files_to_sync:
            # We must schedule this on the asyncio event loop since the watcher runs in a background thread
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.watcher_service.sync_files(files_to_sync))
            except RuntimeError:
                # If no running loop, we can run it synchronously in this thread
                # This usually only happens during tests or unusual shutdown sequences
                asyncio.run(self.watcher_service.sync_files(files_to_sync))

    def _on_event(self, event):
        if event.is_directory:
            return

        filepath = event.src_path

        # Check against exclusions before adding
        if not self.watcher_service._is_supported_file(filepath):
            return

        with self._lock:
            self.changed_files.add(filepath)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._trigger_sync)
            self._timer.start()

    def on_created(self, event):
        self._on_event(event)

    def on_modified(self, event):
        self._on_event(event)

    def on_deleted(self, event):
        # We handle deleted files similarly to modified, the indexer will notice it's gone
        # or we might need special handling if we want to explicitly remove from index.
        # Currently CodebaseIndexer expects to read the file.
        # But `CodeSearchIndex.get_stale_files` handles missing files by yielding them.
        # Let's just track it for sync.
        self._on_event(event)


class FileWatcherService:
    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        self.db_path = get_db_path_for_repo(self.workspace_root)
        self.observer = None
        self._sync_lock = threading.Lock()

        # Load .gitignore patterns (simplified version, we rely on the core exclusion logic)
        from src.core.gitdatacollector import GitDataCollector
        self.git_collector = GitDataCollector()

        from src.core.constants import SUPPORTED_EXTENSIONS
        self.supported_extensions = SUPPORTED_EXTENSIONS

    def _is_supported_file(self, filepath: str) -> bool:
        """Check if a file should be indexed."""
        # Fast path exclusions
        if any(part in filepath for part in ['/.git/', '/node_modules/', '/__pycache__/', '/venv/', '/.venv/']):
            return False

        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.supported_extensions:
            return False

        # Check gitignore
        rel_path = os.path.relpath(filepath, self.workspace_root)
        # Assuming we don't want to re-parse gitignore every time, but checking it once is okay.
        # A more robust check might use `git check-ignore` or the loaded spec.
        return not self.git_collector.is_ignored(rel_path)

    def start(self):
        """Start the file watcher in a background thread."""
        if self.observer is not None:
            return

        if not os.path.isdir(self.workspace_root):
            logger.warning(f"File watcher cannot start: {self.workspace_root} is not a directory")
            return

        event_handler = DebouncedFileEventHandler(self, debounce_seconds=2.0)
        self.observer = Observer()
        self.observer.schedule(event_handler, self.workspace_root, recursive=True)
        self.observer.start()
        logger.info(f"File watcher started on {self.workspace_root}")

    def stop(self):
        """Stop the file watcher."""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5.0)
            self.observer = None
            logger.info("File watcher stopped")

    async def sync_files(self, filepaths: list[str]):
        """Re-index a batch of changed files."""
        # Check which files actually need syncing (some might be fast deletes or temporary)
        valid_filepaths = []
        for path in filepaths:
            # We don't try to index files that don't exist anymore,
            # though we should remove them from the index.
            # Currently `update_index` handles full cleanup. For incremental,
            # let's just index what we can read.
            if os.path.exists(path):
                valid_filepaths.append(path)

        if not valid_filepaths:
            return

        def _do_sync():
            with self._sync_lock:
                logger.info(f"File watcher syncing {len(valid_filepaths)} files...")

                import hashlib

                from src.mcp_server.index_pipeline import CodebaseIndexer

                # Must close cached read-only connections before opening a write connection
                clear_index_cache()

                indexer = CodebaseIndexer(repo_path=self.workspace_root)
                try:
                    for filepath in valid_filepaths:
                        try:
                            content = Path(filepath).read_text(encoding='utf-8', errors='ignore')
                            ext = os.path.splitext(filepath)[1]
                            indexer.index_file(filepath, content, ext, rebuild_fts=False)
                            mtime = os.path.getmtime(filepath)
                            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                            lang = ext.lstrip('.')
                            indexer.search_index.upsert_file_hash(filepath, content_hash, mtime, lang)
                        except Exception as e:
                            logger.warning(f"Watcher auto-sync failed for {filepath}: {e}")
                finally:
                    indexer.close()

                return True

        # Run the sync in a thread to avoid blocking the asyncio loop
        success = await asyncio.to_thread(_do_sync)
        if success:
            clear_index_cache()
            logger.debug("Index cache cleared after watcher sync")
            # Notify clients that resources changed (if any are listening)
            bus.publish("resources_changed", None)

# Global watcher instance
_file_watcher: FileWatcherService = None

def get_file_watcher() -> FileWatcherService:
    return _file_watcher

def init_file_watcher(workspace_root: str) -> FileWatcherService:
    global _file_watcher
    if _file_watcher is not None:
        _file_watcher.stop()
    _file_watcher = FileWatcherService(workspace_root)
    return _file_watcher
