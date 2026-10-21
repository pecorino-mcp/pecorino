import asyncio
import gc
import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import filelock
from mcp.server import ServerRequestContext

from src.core.constants import SUPPORTED_EXTENSIONS as SUPPORTED
from src.core.constants import get_language_for_extension
from src.core.errors import AnalysisError, SecurityValidationError, TargetNotFoundError
from src.mcp_server.context_helper import PecorinoContext
from src.mcp_server.index_db import (
    find_repo_root,
    get_db_path_for_repo,
    get_graph_path_for_repo,
    get_indexes_dir,
)
from src.mcp_server.index_pipeline import CodebaseIndexer
from src.mcp_server.middleware.caching import _API_CACHE, _API_CACHE_LOCK, clear_api_cache
from src.mcp_server.middleware.file_watcher import get_file_watcher
from src.mcp_server.middleware.security import is_safe_path, read_limited, safe_path
from src.mcp_server.registry import registry
from src.mcp_server.tools.browse import do_browse

logger = logging.getLogger(__name__)
workspace_root = Path(__file__).resolve().parent.parent.parent.parent
INDEX_TIMEOUT_S = 3600

_repo_index_locks: dict[str, asyncio.Lock] = {}
_repo_index_locks_guard = threading.Lock()


def _get_repo_lock(repo_root: str) -> asyncio.Lock:
    key = str(Path(repo_root).resolve())
    with _repo_index_locks_guard:
        lock = _repo_index_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _repo_index_locks[key] = lock
        return lock


async def _register_and_summarize(
    repo_root: str,
    target_path: str,
    helper: PecorinoContext | None = None,
    res: dict | None = None,
) -> dict:
    if res is None:
        res = {}
    res["target"] = target_path

    duck_path = get_db_path_for_repo(repo_root)
    graph_path = get_graph_path_for_repo(duck_path)
    registry.register_repo(repo_root, duck_path, graph_path)

    # Notify client that resources have been updated
    if helper is not None:
        await helper.notify_resource_list_changed()

    # Surface FTS errors from the subprocess
    if res.get("status") == "partial" and res.get("fts_error"):
        logger.warning("FTS index rebuild failed: %s", res["fts_error"])
    try:
        summary_res = await do_browse(target=target_path, view="summary")
        res["summary"] = summary_res.get("structure", summary_res)
    except Exception as e:
        logger.warning("Failed to generate summary after indexing: %s", e)

    return res


async def do_update_index(target: str, ctx: ServerRequestContext | None = None, allow_external: bool = False) -> dict:
    helper = PecorinoContext(ctx)
    path = safe_path(target, allow_external)

    repo_root = find_repo_root(str(path))
    repo_root_path = Path(repo_root).resolve()
    if not is_safe_path(str(repo_root_path), allow_external):
        raise SecurityValidationError(f"Repository root blocked by security rules: {repo_root_path}")

    repo_lock = _get_repo_lock(str(repo_root_path))
    repo_hash = hashlib.md5(str(repo_root_path).encode("utf-8")).hexdigest()
    lock_file_path = Path(get_indexes_dir()) / f"{repo_hash}.index.lock"
    file_lock = filelock.FileLock(str(lock_file_path), timeout=INDEX_TIMEOUT_S)

    async with repo_lock:
        await asyncio.to_thread(file_lock.acquire)
        try:
            # Invalidate pagerank cache on the existing GraphAPI if cached, before clearing
            try:
                _db_path = get_db_path_for_repo(repo_root)
                with _API_CACHE_LOCK:
                    cached_graph = _API_CACHE.get((_db_path, "graph"))
                    if cached_graph:
                        cached_graph.invalidate_pagerank_cache()
            except Exception:
                pass  # Best-effort invalidation before full cache clear
            clear_api_cache()
            # Force garbage collection to release any lingering __del__ database connections
            # that may hold write locks and block the indexing subprocess.
            # TODO: Audit whether this is still needed now that CodebaseIndexer uses a
            # context manager. Keep as a safety net for other potential leaked connections.
            gc.collect()

            # Stop the background file watcher during indexing to prevent conflicting locks
            watcher = get_file_watcher()
            if watcher:
                watcher.stop()

            try:
                if path.is_dir():
                    venv_python = workspace_root / ".venv" / "bin" / "python"
                    if venv_python.exists():
                        python_bin = str(venv_python)
                    else:
                        python_bin = sys.executable or "python"

                    env = dict(os.environ)
                    env["PECORINO_USE_RAMDISK"] = "false"

                    proc = await asyncio.create_subprocess_exec(
                        python_bin, "-u", "-m", "src.mcp_server.index_pipeline", repo_root, str(path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=str(workspace_root),
                        env=env
                    )

                    final_res = {}
                    start_time = time.time()

                    async def read_stdout():
                        nonlocal final_res
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            line_str = line.decode("utf-8", errors="ignore").strip()
                            if not line_str:
                                continue
                            try:
                                data = json.loads(line_str)
                                if "result" in data:
                                    final_res = data["result"]
                                elif "current" in data and "total" in data:
                                    current = data["current"]
                                    total = data["total"]
                                    from src.utils.helpers import print_progress_bar
                                    filename = os.path.basename(data.get("file", ""))
                                    msg = f"Indexing {filename} ({current}/{total})"
                                    print_progress_bar(
                                        current,
                                        total,
                                        prefix="[INFO] Indexing",
                                        suffix=f"({current}/{total}) {filename[:30]:<30}",
                                        stream=sys.stderr,
                                        start_time=start_time
                                    )

                                    await helper.report_progress(
                                        progress=current,
                                        total=total,
                                        message=msg
                                    )
                            except Exception as e:
                                logger.warning("Subprocess parse error: %s for line: %s", e, line_str)

                    stderr_logs = []
                    async def read_stderr():
                        while True:
                            line = await proc.stderr.readline()
                            if not line:
                                break
                            line_str = line.decode("utf-8", errors="ignore").strip()
                            if line_str:
                                stderr_logs.append(line_str)
                                logger.debug("[index worker] %s", line_str)

                    try:
                        await asyncio.wait_for(
                            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
                            timeout=INDEX_TIMEOUT_S
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        raise AnalysisError(f"Indexing timed out after {INDEX_TIMEOUT_S}s") from None

                    if proc.returncode != 0:
                        err_msg = "\n".join(stderr_logs[-50:])
                        raise AnalysisError(f"Index subprocess failed with exit code {proc.returncode}. Stderr: {err_msg}")

                    return await _register_and_summarize(repo_root, path.as_posix(), helper, final_res)

                elif path.is_file():
                    is_adr = path.suffix == ".md" and "docs/adr" in path.as_posix()
                    if path.suffix not in SUPPORTED and not is_adr:
                        raise SecurityValidationError(f"Unsupported extension: {path.suffix}")

                    content = await asyncio.to_thread(read_limited, path)

                    def _index_file():
                        with CodebaseIndexer(repo_path=repo_root) as indexer:
                            indexer.index_file(str(path), content, path.suffix, rebuild_fts=False)
                            mtime = os.path.getmtime(str(path))
                            md5_hash = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
                            lang = get_language_for_extension(path.suffix)
                            indexer.search_index.upsert_file_hash(str(path), md5_hash, mtime, lang)

                    await asyncio.to_thread(_index_file)

                    res = {"status": "success", "target": path.as_posix(), "indexed_files": 1, "total_files_found": 1}
                    return await _register_and_summarize(repo_root, path.as_posix(), helper, res)

                else:
                    raise TargetNotFoundError(f"Target is neither a file nor a directory: {path}")

            finally:
                if watcher:
                    watcher.start()
        finally:
            try:
                file_lock.release()
            except Exception:
                pass
