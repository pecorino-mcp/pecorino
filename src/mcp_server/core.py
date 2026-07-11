import logging
import os
import sys
import threading
from pathlib import Path

from mcp.server.subscriptions import ListenHandler

_fts_rebuild_lock = threading.Lock()
_auto_sync_lock = threading.Lock()


logger = logging.getLogger(__name__)
from mcp.server import Server, ServerRequestContext

from src.core.constants import SUPPORTED_EXTENSIONS
from src.mcp_server.middleware.input_required import InputRequiredMiddleware

SUPPORTED = SUPPORTED_EXTENSIONS

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

# --- Security constants (Moved to middleware/security.py) ---

ALLOWED_VIEWS = frozenset({"summary", "classes", "functions", "deps", "tree",
                           "pagerank", "all"})
ALLOWED_WHAT = frozenset({"oop", "complexity", "hotspots", "all"})
ALLOWED_API_TYPES = frozenset({"index", "graph"})
MAX_LIMIT = 100
MAX_DEPTH = 10
MAX_QUERY_LEN = 200
MAX_CODE_LINES = 300  # Max lines of source code returned per result in 'code' view
INDEX_TIMEOUT_S = 300  # 5 minutes





# Core implementation of tools (without decorators)




# Low-level Handlers


class RoleMiddleware:
    async def __call__(self, ctx: ServerRequestContext, call_next):
        role = os.environ.get("MCP_USER_ROLE", "admin")
        if not hasattr(ctx, "lifespan_context") or ctx.lifespan_context is None:
            ctx.lifespan_context = {}
        ctx.lifespan_context["user_role"] = role
        return await call_next(ctx)

from src.mcp_server.events import bus
from src.mcp_server.handlers.prompts import (
    handle_completion,
    handle_get_prompt,
    handle_list_prompts,
)
from src.mcp_server.handlers.resources import handle_list_resources, handle_read_resource
from src.mcp_server.handlers.tools import handle_call_tool, handle_list_tools

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



