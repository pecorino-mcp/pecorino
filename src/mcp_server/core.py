import asyncio
import collections
import glob
import json
import os
import sys
import logging
from mcp.server.subscriptions import ListenHandler
import threading
import time
from pathlib import Path

_fts_rebuild_lock = threading.Lock()
_auto_sync_lock = threading.Lock()
from typing import Any, List, Optional

import mcp_types as types

logger = logging.getLogger(__name__)
from mcp.server import Server, ServerRequestContext

from src.mcp_server.context_helper import PecorinoContext
from src.mcp_server.middleware.input_required import InputRequiredMiddleware
from src.core.gitdatacollector import GitDataCollector
from src.mcp_server.prometheus_metrics import TOOL_CALLS, TOOL_DURATION, TOOL_ERRORS
from src.metrics.hotspot import HotspotDetector
from src.metrics.maintainability import (
    calculate_halstead_metrics,
    calculate_loc_metrics,
    calculate_maintainability_index,
    calculate_mccabe_complexity,
)
from src.metrics.oopmetrics import OOPMetricsAnalyzer, parse
from src.utils.export import MetricsExporter

from src.core.constants import SUPPORTED_EXTENSIONS

SUPPORTED = SUPPORTED_EXTENSIONS

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

# --- Security constants (Moved to middleware/security.py) ---
from src.mcp_server.middleware.security import (
    ALLOWED_OUTPUT,
    MAX_READ_BYTES,
    STRICT_INJECTION_CHECK,
    SUSPICIOUS_PATTERNS
)

ALLOWED_VIEWS = frozenset({"summary", "classes", "functions", "deps", "tree", "search",
                           "callers", "callees", "impact", "pagerank", "functional-analysis", "code"})
ALLOWED_WHAT = frozenset({"oop", "complexity", "hotspots", "all"})
ALLOWED_API_TYPES = frozenset({"index", "graph"})
MAX_LIMIT = 100
MAX_DEPTH = 10
MAX_QUERY_LEN = 200
MAX_CODE_LINES = 300  # Max lines of source code returned per result in 'code' view
INDEX_TIMEOUT_S = 300  # 5 minutes

from src.mcp_server.config import settings
from src.core.errors import (
    PecorinoError,
    SecurityValidationError,
    TargetNotFoundError,
    IndexNotFoundError,
    AnalysisError
)
from src.mcp_server.errors import handle_mcp_error


from src.mcp_server.middleware.security import (
    is_project_workspace,
    is_safe_path,
    safe_path,
    safe_output_path,
    read_limited,
    check_suspicious
)

from src.mcp_server.middleware.caching import (
    _get_cached_api,
    clear_api_cache,
    clear_index_cache
)

# Core implementation of tools (without decorators)

from src.mcp_server.middleware.sync import _auto_sync_stale

from src.mcp_server.tools.browse import do_browse
from src.mcp_server.tools.metrics_tool import do_metrics
from src.mcp_server.tools.update_index import do_update_index


# Low-level Handlers


class RoleMiddleware:
    async def __call__(self, ctx: ServerRequestContext, call_next):
        role = os.environ.get("MCP_USER_ROLE", "admin")
        if not hasattr(ctx, "lifespan_context") or ctx.lifespan_context is None:
            ctx.lifespan_context = {}
        ctx.lifespan_context["user_role"] = role
        return await call_next(ctx)

from src.mcp_server.handlers.tools import handle_list_tools, handle_call_tool
from src.mcp_server.handlers.prompts import handle_list_prompts, handle_get_prompt, handle_completion
from src.mcp_server.handlers.resources import handle_list_resources, handle_read_resource
from src.mcp_server.events import bus
server = Server(
    "OOP Metrics Analyzer Server 🚀",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
    on_list_prompts=handle_list_prompts,
    on_get_prompt=handle_get_prompt,
    on_completion=handle_completion,
    on_list_resources=handle_list_resources,
    on_read_resource=handle_read_resource,
    on_subscriptions_listen=ListenHandler(bus),
)
server.middleware.append(RoleMiddleware())
server.middleware.append(InputRequiredMiddleware())



