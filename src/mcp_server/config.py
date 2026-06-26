import os

class Config:
    def __init__(self):
        # Determine Transport Mode (stdio, sse, streamable-http)
        self.transport = os.getenv("MCP_TRANSPORT", "stdio")
        
        # Determine Host/Port for Network Transports
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))
        
        # OAuth 2.1 Configurations
        self.oauth_jwt_secret = os.getenv("OAUTH_JWT_SECRET", "pecorino-secret-key-change-in-prod")
        self.oauth_resource = os.getenv("OAUTH_RESOURCE", "pecorino://mcp-server")
        self.oauth_issuer = os.getenv("OAUTH_ISSUER", "https://auth.pecorino.com")
        self.oauth_required = os.getenv("OAUTH_REQUIRED", "true").lower() in ("true", "1", "yes")

# Global singleton configuration
settings = Config()
