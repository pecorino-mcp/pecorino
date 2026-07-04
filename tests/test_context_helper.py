import pytest
import json
import mcp_types as types
from src.mcp_server.context_helper import PecorinoContext, NeedsInputError
from src.mcp_server.middleware.input_required import InputRequiredMiddleware

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
