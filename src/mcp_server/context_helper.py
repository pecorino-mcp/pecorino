import json
import os
import logging
from typing import Any, Optional, Dict, List

from mcp.server.context import ServerRequestContext
import mcp_types as types

logger = logging.getLogger(__name__)


class NeedsInputError(Exception):
    """Raised by a tool handler to signal that client input is required."""

    def __init__(
        self,
        input_requests: dict[str, types.InputRequest],
        request_state: dict[str, Any] | None = None,
    ):
        self.input_requests = input_requests
        # SDK's InputRequiredResult.request_state is str|None,
        # so we JSON-serialize any structured state.
        self.request_state = json.dumps(request_state) if request_state else None


class PecorinoContext:
    """Unified context wrapper for MCP handlers and standalone CLI usage."""

    def __init__(
        self,
        ctx: Optional[ServerRequestContext] = None,
        input_responses: Optional[dict[str, Any]] = None,
    ):
        self._ctx = ctx
        self._input_responses = input_responses or {}

    # ── Identity ──────────────────────────────────────────────

    @property
    def role(self) -> str:
        if self._ctx:
            return getattr(self._ctx, "lifespan_context", {}).get("user_role", "admin")
        return os.environ.get("MCP_USER_ROLE", "admin")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def client_name(self) -> str:
        if not self._ctx:
            return "standalone"
        cp = self._ctx.session.client_params
        if cp and cp.clientInfo:
            return cp.clientInfo.name
        return "unknown"

    @property
    def protocol_version(self) -> str:
        if not self._ctx:
            return "standalone"
        return self._ctx.protocol_version

    @property
    def is_modern_protocol(self) -> bool:
        if not self._ctx:
            return False
        return self.protocol_version >= "2026-07-28"

    # ── Progress ──────────────────────────────────────────────

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """Report progress. Falls back to logging when not in an MCP session."""
        if not self._ctx:
            logger.info("Progress: %s/%s — %s", progress, total, message)
            return
        try:
            # ctx.session.report_progress is a no-op when the caller
            # didn't request progress (no progressToken in _meta).
            await self._ctx.session.report_progress(progress, total, message)
        except Exception as e:
            logger.warning("Failed to report progress: %s", e)

    # ── Multi-Round-Trip Inputs ───────────────────────────────

    def get_input_response(self, key: str) -> Any | None:
        """Get a client response from a prior InputRequiredResult round-trip."""
        return self._input_responses.get(key)

    async def require_roots(self) -> list[Any]:
        """Get workspace roots from the client.

        - If roots were already provided via input_responses, returns them.
        - On modern protocol (2026-07-28): raises NeedsInputError.
        - On legacy protocol: sends a direct server→client ListRootsRequest.
        - In standalone mode: returns [].
        """
        # Already have roots from a previous round-trip?
        if "workspace_roots" in self._input_responses:
            result = self._input_responses["workspace_roots"]
            if isinstance(result, types.ListRootsResult):
                return result.roots
            # Fallback: raw dict
            return result.get("roots", []) if isinstance(result, dict) else []

        if not self._ctx:
            return []

        if self.is_modern_protocol:
            # Modern protocol: server cannot push requests → raise for middleware
            raise NeedsInputError(
                input_requests={"workspace_roots": types.ListRootsRequest()},
                request_state={"step": "require_roots"},
            )
        else:
            # Legacy protocol: direct server→client request via Connection overloads
            try:
                result = await self._ctx.session.send_request(
                    types.ListRootsRequest(),
                    types.ListRootsResult,
                )
                return result.roots
            except Exception as e:
                logger.warning("Failed to fetch roots (legacy): %s", e)
                return []
