import json
import logging
import os
import time
import uuid
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)

# Cache for the log path so we don't recompute it every time
_LOG_FILE_PATH: Optional[str] = None

def get_telemetry_log_path() -> str:
    global _LOG_FILE_PATH
    if _LOG_FILE_PATH is not None:
        return _LOG_FILE_PATH

    try:
        from src.mcp_server.config import settings
        log_dir = settings.index_dir.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE_PATH = str(log_dir / "search_events.log")
        return _LOG_FILE_PATH
    except Exception as e:
        logger.warning(f"Could not resolve telemetry log path: {e}")
        # Fallback to local directory
        _LOG_FILE_PATH = os.path.abspath("search_events.log")
        return _LOG_FILE_PATH

def _append_log(log_path: str, line: str):
    with open(log_path, mode='a', encoding='utf-8') as f:
        f.write(line + "\n")

async def log_search_event(
    original_query: str,
    final_mode: str,
    latency_ms: float,
    result_count: int,
    intent: Optional[str] = None,
    repo_root: Optional[str] = None
):
    """
    Log a search event to a local .log file (JSONL format).
    Each event has a unique UUID and is written as a single line (one event per tick).
    """
    try:
        log_path = get_telemetry_log_path()

        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "original_query": original_query,
            "mode": final_mode,
            "intent": intent,
            "latency_ms": round(latency_ms, 2),
            "result_count": result_count,
            "repo_root": repo_root
        }

        line = json.dumps(event)
        await asyncio.to_thread(_append_log, log_path, line)

    except Exception as e:
        logger.warning(f"Failed to log search telemetry: {e}")
