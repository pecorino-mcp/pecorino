import logging

import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from src.mcp_server.context_helper import NeedsInputError

logger = logging.getLogger(__name__)


class InputRequiredMiddleware:
    """Intercepts NeedsInputError and returns InputRequiredResult."""

    async def __call__(
        self,
        ctx: ServerRequestContext,
        call_next: CallNext,
    ) -> HandlerResult:
        try:
            return await call_next(ctx)
        except NeedsInputError as exc:
            logger.info(
                "Tool requested client input: %s",
                list(exc.input_requests.keys()),
            )
            return types.InputRequiredResult(
                input_requests=exc.input_requests,
                request_state=exc.request_state,
            )
