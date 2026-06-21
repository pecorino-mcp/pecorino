import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from fastapi.exceptions import HTTPException

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.mcp_server.config import settings
from src.mcp_server.metrics import ACTIVE_SESSIONS
from src.transports.auth import verify_oauth_token

async def oauth_middleware(request: Request, call_next):
    try:
        verify_oauth_token(request)
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": "invalid_token", "error_description": e.detail},
            headers=e.headers
        )
    return await call_next(request)

def create_base_app(title: str) -> FastAPI:
    app = FastAPI(title=title)
    app.middleware("http")(oauth_middleware)
    
    @app.get("/metrics")
    async def handle_metrics(request: Request):
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)
        
    return app

async def run_sse(mcp_server):
    from mcp.server.sse import SseServerTransport
    from mcp.server.models import InitializationOptions
    from mcp.server.lowlevel import NotificationOptions
    
    transport = SseServerTransport("/messages/")
    app = create_base_app("gitstats3 MCP Server (SSE)")

    async def handle_sse(request: Request):
        ACTIVE_SESSIONS.inc()
        try:
            async with transport.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="gitstats3",
                        server_version="3.0.0",
                        capabilities=mcp_server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    )
                )
        finally:
            ACTIVE_SESSIONS.dec()
        return Response()

    app.add_route("/sse", handle_sse, methods=["GET"])
    app.mount("/messages/", transport.handle_post_message)

    config = uvicorn.Config(app, host=settings.host, port=settings.port)
    server_uv = uvicorn.Server(config)
    await server_uv.serve()


async def run_streamable_http(mcp_server):
    app = create_base_app("gitstats3 MCP Server (Streamable HTTP)")

    streamable_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True
    )

    app.mount("/mcp", streamable_app)

    config = uvicorn.Config(app, host=settings.host, port=settings.port)
    server_uv = uvicorn.Server(config)
    await server_uv.serve()
