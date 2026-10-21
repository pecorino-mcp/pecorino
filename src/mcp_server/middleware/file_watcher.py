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

    def has_pending_files(self) -> bool:
        with self._lock:
            return bool(self.changed_files) or self._timer is not None

    def cancel_timer(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.changed_files.clear()

    def _trigger_sync(self):
        with self._lock:
            files_to_sync = list(self.changed_files)
            self.changed_files.clear()
            self._timer = None

        if files_to_sync:
            loop = getattr(self.watcher_service, "_loop", None)
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.watcher_service.sync_files(files_to_sync), loop
                )
            else:
                try:
                    running_loop = asyncio.get_running_loop()
                    running_loop.create_task(self.watcher_service.sync_files(files_to_sync))
                except RuntimeError:
                    # Run directly on the timer thread without spinning up a throwaway event loop
                    self.watcher_service._do_sync_sync(files_to_sync)

    def _queue_path(self, filepath: str):
        if not filepath or not self.watcher_service._is_supported_file(filepath):
            return

        with self._lock:
            self.changed_files.add(filepath)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._trigger_sync)
            self._timer.daemon = True
            self._timer.start()

    def _on_event(self, event):
        if event.is_directory:
            return
        self._queue_path(event.src_path)

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

    def on_moved(self, event):
        if event.is_directory:
            return
        self._queue_path(getattr(event, "src_path", None))
        self._queue_path(getattr(event, "dest_path", None))


class FileWatcherService:
    def __init__(self, workspace_root: str):
        self.workspace_root = str(workspace_root)
        self.db_path = get_db_path_for_repo(self.workspace_root)
        self.observer = None
        self._handler: DebouncedFileEventHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sync_lock = threading.Lock()

        from src.core.constants import SUPPORTED_EXTENSIONS
        self.supported_extensions = SUPPORTED_EXTENSIONS

    def is_active_for(self, repo_root: str) -> bool:
        """Check whether this watcher is actively watching repo_root with no pending events."""
        if self.observer is None or not self.observer.is_alive():
            return False
        if os.path.abspath(str(self.workspace_root)) != os.path.abspath(str(repo_root)):
            return False
        if self._handler is not None and self._handler.has_pending_files():
            return False
        return True

    def _is_supported_file(self, filepath: str) -> bool:
        """Check if a file should be indexed."""
        # Fast path exclusions
        if any(part in filepath for part in ['/.git/', '/node_modules/', '/__pycache__/', '/venv/', '/.venv/']):
            return False

        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.supported_extensions:
            return False

        # Check gitignore
        import subprocess
        rel_path = os.path.relpath(filepath, self.workspace_root)
        try:
            res = subprocess.run(
                ["git", "check-ignore", "-q", rel_path],
                cwd=self.workspace_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return res.returncode != 0
        except Exception:
            return True

    def start(self):
        """Start the file watcher in a background thread."""
        if self.observer is not None:
            return

        if not os.path.isdir(self.workspace_root):
            logger.warning(f"File watcher cannot start: {self.workspace_root} is not a directory")
            return

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        self._handler = DebouncedFileEventHandler(self, debounce_seconds=2.0)
        self.observer = Observer()
        self.observer.schedule(self._handler, self.workspace_root, recursive=True)
        self.observer.start()
        logger.info(f"File watcher started on {self.workspace_root}")

    def stop(self):
        """Stop the file watcher."""
        if self._handler is not None:
            self._handler.cancel_timer()
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5.0)
            self.observer = None
            logger.info("File watcher stopped")

    def _do_sync_sync(self, filepaths: list[str]) -> bool:
        """Synchronously re-index modified files and clear deleted files."""
        if not filepaths:
            return False

        existing_filepaths = []
        deleted_filepaths = []
        for path in filepaths:
            if os.path.exists(path):
                existing_filepaths.append(path)
            else:
                deleted_filepaths.append(path)

        if not existing_filepaths and not deleted_filepaths:
            return False

        with self._sync_lock:
            logger.info(
                f"File watcher syncing {len(existing_filepaths)} modified and {len(deleted_filepaths)} deleted files..."
            )

            import hashlib

            from src.core.constants import get_language_for_extension
            from src.mcp_server.index_pipeline import CodebaseIndexer

            # Must close cached read-only connections before opening a write connection
            clear_index_cache()

            changed = False
            indexer = CodebaseIndexer(repo_path=self.workspace_root)
            try:
                for filepath in deleted_filepaths:
                    try:
                        indexer.search_index.clear_file(filepath)
                        changed = True
                    except Exception as e:
                        logger.warning(f"Watcher auto-sync clear failed for {filepath}: {e}")

                for filepath in existing_filepaths:
                    try:
                        content = Path(filepath).read_text(encoding='utf-8', errors='ignore')
                        ext = os.path.splitext(filepath)[1]
                        indexer.index_file(filepath, content, ext, rebuild_fts=False)
                        mtime = os.path.getmtime(filepath)
                        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                        lang = get_language_for_extension(ext)
                        indexer.search_index.upsert_file_hash(filepath, content_hash, mtime, lang)
                        changed = True
                    except Exception as e:
                        logger.warning(f"Watcher auto-sync failed for {filepath}: {e}")

                if changed:
                    indexer.search_index.mark_fts_dirty()
                    indexer.search_index.rebuild_fts()
            finally:
                indexer.close()

            if changed:
                clear_index_cache()
                logger.debug("Index cache cleared after watcher sync")
            return changed

    async def sync_files(self, filepaths: list[str]):
        """Re-index a batch of changed files."""
        if not filepaths:
            return

        # Run the sync in a thread to avoid blocking the asyncio loop
        success = await asyncio.to_thread(self._do_sync_sync, filepaths)
        if success:
            from mcp.shared.subscriptions import ResourcesListChanged
            await bus.publish(ResourcesListChanged())
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
