import pytest
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import mcp_types as types
from src.mcp_server.context_helper import PecorinoContext, NeedsInputError
from src.mcp_server.middleware.input_required import InputRequiredMiddleware

# ── Existing tests (preserved) ────────────────────────────────

@pytest.mark.asyncio
async def test_pecorino_context_fallback(caplog):
    """Test PecorinoContext when no ServerRequestContext is provided (standalone)."""
    import logging
    helper = PecorinoContext(ctx=None)
    
    assert helper.role in ("admin", "viewer")
    assert helper.client_name == "standalone"
    assert helper.protocol_version == "standalone"
    assert not helper.is_modern_protocol
    
    # Should log the progress in fallback mode
    with caplog.at_level(logging.INFO):
        await helper.report_progress(50, 100, "test progress")
    assert "Progress: 50/100 — test progress" in caplog.text
    
    # In standalone, require_roots should just return empty list
    roots = await helper.require_roots()
    assert roots == []

@pytest.mark.asyncio
async def test_require_roots_modern_protocol():
    """Test require_roots raises NeedsInputError on modern protocol if not already provided."""
    class MockCtx:
        protocol_version = "2026-07-28"
    
    helper = PecorinoContext(ctx=MockCtx()) # type: ignore
    
    with pytest.raises(NeedsInputError) as exc_info:
        await helper.require_roots()
        
    assert "workspace_roots" in exc_info.value.input_requests
    assert exc_info.value.request_state == json.dumps({"step": "require_roots"})

@pytest.mark.asyncio
async def test_require_roots_with_input_responses():
    """Test require_roots returns cached roots when provided in input_responses."""
    helper = PecorinoContext(
        ctx=None, 
        input_responses={
            "workspace_roots": {"roots": [{"uri": "file:///test/repo", "name": "repo"}]}
        }
    )
    
    roots = await helper.require_roots()
    assert len(roots) == 1
    assert roots[0]["uri"] == "file:///test/repo"

@pytest.mark.asyncio
async def test_input_required_middleware(caplog):
    """Test middleware intercepts NeedsInputError and returns InputRequiredResult."""
    import logging
    middleware = InputRequiredMiddleware()
    
    async def failing_handler(ctx):
        raise NeedsInputError(
            input_requests={"test": types.ListRootsRequest()},
            request_state={"state": "123"}
        )
    
    with caplog.at_level(logging.INFO):
        result = await middleware(None, failing_handler) # type: ignore
    assert isinstance(result, types.InputRequiredResult)
    assert "test" in result.input_requests
    assert result.request_state == json.dumps({"state": "123"})
    assert "Tool requested client input: ['test']" in caplog.text

# ── New tests for reworked PecorinoContext ─────────────────────

@pytest.mark.asyncio
async def test_log_standalone_fallback(caplog):
    """In standalone mode, log() delegates to Python logging."""
    helper = PecorinoContext(ctx=None)
    
    with caplog.at_level(logging.DEBUG):
        await helper.log("info", "test message", logger_name="pecorino.test")
    
    assert "test message" in caplog.text

@pytest.mark.asyncio
async def test_log_with_session(caplog):
    """Even with a session, log() uses Python logging (SEP-2577 deprecation)."""
    mock_ctx = MagicMock()
    mock_ctx.request_id = "req-42"
    
    helper = PecorinoContext(ctx=mock_ctx)
    
    with caplog.at_level(logging.WARNING):
        await helper.log("warning", "test warning", logger_name="pecorino.tools")
    
    assert "test warning" in caplog.text

@pytest.mark.asyncio
async def test_log_all_levels(caplog):
    """log() maps MCP levels to Python logging levels correctly."""
    helper = PecorinoContext(ctx=None)
    
    with caplog.at_level(logging.DEBUG):
        await helper.log("debug", "dbg msg")
        await helper.log("error", "err msg")
    
    assert "dbg msg" in caplog.text
    assert "err msg" in caplog.text

@pytest.mark.asyncio
async def test_has_capability_standalone():
    """In standalone mode, has_capability always returns False."""
    helper = PecorinoContext(ctx=None)
    assert helper.has_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())) is False
    assert helper.supports_elicitation is False
    assert helper.supports_sampling is False

@pytest.mark.asyncio
async def test_has_capability_with_session():
    """has_capability delegates to session.check_client_capability."""
    mock_session = MagicMock()
    mock_session.check_client_capability = MagicMock(return_value=True)
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    
    helper = PecorinoContext(ctx=mock_ctx)
    assert helper.has_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())) is True

@pytest.mark.asyncio
async def test_elicit_standalone():
    """In standalone mode, elicit() returns None."""
    helper = PecorinoContext(ctx=None)
    result = await helper.elicit("confirm?", {"type": "object", "properties": {}})
    assert result is None

@pytest.mark.asyncio
async def test_elicit_unsupported_client():
    """When the client doesn't support elicitation, elicit() returns None."""
    mock_session = MagicMock()
    mock_session.check_client_capability = MagicMock(return_value=False)
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    
    helper = PecorinoContext(ctx=mock_ctx)
    result = await helper.elicit("confirm?", {"type": "object"})
    assert result is None

@pytest.mark.asyncio
async def test_elicit_supported_client():
    """When the client supports elicitation, elicit() calls session.elicit."""
    mock_result = MagicMock(spec=types.ElicitResult)
    mock_result.action = "accept"
    
    mock_session = MagicMock()
    mock_session.check_client_capability = MagicMock(return_value=True)
    mock_session.elicit = AsyncMock(return_value=mock_result)
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    mock_ctx.request_id = "req-99"
    
    helper = PecorinoContext(ctx=mock_ctx)
    result = await helper.elicit("confirm?", {"type": "object"})
    
    assert result is mock_result
    mock_session.elicit.assert_awaited_once()

@pytest.mark.asyncio
async def test_notify_standalone_noop():
    """In standalone mode, notification methods are safe no-ops."""
    helper = PecorinoContext(ctx=None)
    # Should not raise
    await helper.notify_tool_list_changed()
    await helper.notify_resource_list_changed()

@pytest.mark.asyncio
async def test_notify_with_session():
    """When a session exists, notifications call the session methods."""
    mock_session = MagicMock()
    mock_session.send_tool_list_changed = AsyncMock()
    mock_session.send_resource_list_changed = AsyncMock()
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    
    helper = PecorinoContext(ctx=mock_ctx)
    
    await helper.notify_tool_list_changed()
    mock_session.send_tool_list_changed.assert_awaited_once()
    
    await helper.notify_resource_list_changed()
    mock_session.send_resource_list_changed.assert_awaited_once()

@pytest.mark.asyncio
async def test_request_id_tracking():
    """request_id is extracted from the context."""
    mock_ctx = MagicMock()
    mock_ctx.request_id = "req-123"
    
    helper = PecorinoContext(ctx=mock_ctx)
    assert helper.request_id == "req-123"

@pytest.mark.asyncio
async def test_request_id_standalone():
    """request_id is None in standalone mode."""
    helper = PecorinoContext(ctx=None)
    assert helper.request_id is None

@pytest.mark.asyncio
async def test_create_message_standalone():
    """In standalone mode, create_message() returns None."""
    helper = PecorinoContext(ctx=None)
    result = await helper.create_message(messages=[], max_tokens=100)
    assert result is None

@pytest.mark.asyncio
async def test_create_message_unsupported_client():
    """When the client doesn't support sampling, create_message() returns None."""
    mock_session = MagicMock()
    mock_session.check_client_capability = MagicMock(return_value=False)
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    
    helper = PecorinoContext(ctx=mock_ctx)
    result = await helper.create_message(messages=[], max_tokens=100)
    assert result is None
