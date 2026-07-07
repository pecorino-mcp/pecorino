import json
import os
import logging
from typing import Any, Optional, Dict, List, Literal

from mcp.server.context import ServerRequestContext
import mcp_types as types

logger = logging.getLogger(__name__)

# Valid MCP log levels matching the LoggingLevel type
LogLevel = Literal[
    "debug", "info", "notice", "warning",
    "error", "critical", "alert", "emergency",
]

# Map MCP log levels to Python logging levels for standalone fallback
_MCP_TO_PYTHON_LOG = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "alert": logging.CRITICAL,
    "emergency": logging.CRITICAL,
}


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
    """Unified context wrapper for MCP handlers and standalone CLI usage.

    Wraps the raw ``ServerRequestContext`` and provides gracefully-degrading
    helpers for every MCP session capability:

    * **Identity** — role, client name, protocol version
    * **Progress** — ``report_progress``
    * **Structured logging** — ``log``
    * **Elicitation** — ``elicit``
    * **Sampling** — ``create_message``
    * **Capability checking** — ``has_capability``
    * **Notifications** — ``notify_tool_list_changed``, ``notify_resource_list_changed``
    * **Multi-round inputs** — ``require_roots``, ``get_input_response``

    Every method is a safe no-op or returns a sensible default when called
    outside an active MCP session (standalone / CLI mode).
    """

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

    @property
    def request_id(self) -> Any:
        """The JSON-RPC request ID for the current inbound request, or None."""
        if not self._ctx:
            return None
        return getattr(self._ctx, "request_id", None)

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

    # ── Structured Logging ────────────────────────────────────

    async def log(
        self,
        level: LogLevel,
        data: Any,
        logger_name: str | None = None,
    ) -> None:
        """Log a message using Python's standard ``logging`` module.

        This is a convenience wrapper that standardizes log format across
        the codebase.  It always uses Python logging (to stderr), not the
        MCP protocol's ``send_log_message`` — that capability is deprecated
        by SEP-2577 (2026-07-28 spec) with no in-protocol replacement.
        """
        py_level = _MCP_TO_PYTHON_LOG.get(level, logging.INFO)
        log_target = logging.getLogger(logger_name) if logger_name else logger
        log_target.log(py_level, "%s", data)

    # ── Capability Checking ───────────────────────────────────

    def has_capability(self, capability: types.ClientCapabilities) -> bool:
        """Check whether the connected client advertises a given capability.

        Returns ``False`` in standalone mode or when the check fails.
        """
        if not self._ctx:
            return False
        try:
            return self._ctx.session.check_client_capability(capability)
        except Exception:
            return False

    @property
    def supports_elicitation(self) -> bool:
        """Shorthand: does the client support form elicitation?"""
        return self.has_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability()))

    @property
    def supports_sampling(self) -> bool:
        """Shorthand: does the client support sampling / create_message?"""
        return self.has_capability(types.ClientCapabilities(sampling=types.SamplingCapability()))

    # ── Elicitation ───────────────────────────────────────────

    async def elicit(
        self,
        message: str,
        requested_schema: dict[str, Any],
    ) -> types.ElicitResult | None:
        """Ask the client for structured input via an elicitation form.

        Returns the ``ElicitResult`` on success, or ``None`` when:
        * running in standalone mode,
        * the client does not support elicitation, or
        * the request fails.
        """
        if not self._ctx:
            return None
        if not self.supports_elicitation:
            logger.debug("Client does not support elicitation; skipping prompt")
            return None
        try:
            return await self._ctx.session.elicit(
                message=message,
                requested_schema=requested_schema,
                related_request_id=self.request_id,
            )
        except Exception as e:
            logger.warning("Elicitation failed: %s", e)
            return None

    # ── Sampling ──────────────────────────────────────────────

    async def create_message(
        self,
        messages: list[types.SamplingMessage],
        *,
        max_tokens: int = 1024,
        system_prompt: str | None = None,
        include_context: Literal["none", "thisServer", "allServers"] | None = None,
        temperature: float | None = None,
        model_preferences: types.ModelPreferences | None = None,
    ) -> types.CreateMessageResult | None:
        """Request the client to sample an LLM on the server's behalf.

        .. deprecated:: SEP-2577 (2026-07-28)
            Server-initiated sampling is deprecated.  On modern-protocol
            connections this call will fail because no back-channel exists.
            Prefer ``InputRequiredResult`` multi-round-trip instead.

        Returns ``None`` in standalone mode or when the client does not
        support sampling.
        """
        if not self._ctx:
            return None
        if not self.supports_sampling:
            logger.debug("Client does not support sampling; skipping")
            return None
        try:
            return await self._ctx.session.create_message(
                messages=messages,
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                include_context=include_context,
                temperature=temperature,
                model_preferences=model_preferences,
                related_request_id=self.request_id,
            )
        except Exception as e:
            logger.warning("Sampling request failed: %s", e)
            return None

    # ── Notifications ─────────────────────────────────────────

    async def notify_tool_list_changed(self) -> None:
        """Notify the client that the list of available tools has changed."""
        if not self._ctx:
            return
        try:
            await self._ctx.session.send_tool_list_changed()
        except Exception as e:
            logger.debug("Failed to send tool list changed notification: %s", e)

    async def notify_resource_list_changed(self) -> None:
        """Notify the client that the list of available resources has changed."""
        if not self._ctx:
            return
        try:
            await self._ctx.session.send_resource_list_changed()
        except Exception as e:
            logger.debug("Failed to send resource list changed notification: %s", e)

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
