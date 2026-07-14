import os
from pathlib import Path


class Config:
    def __init__(self):
        # Determine Transport Mode (stdio, sse, streamable-http)
        self.transport = os.getenv("MCP_TRANSPORT", "stdio")

        # Determine Host/Port for Network Transports
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))

        # Concurrency controls
        self.max_concurrent_tools = int(os.getenv("MCP_MAX_CONCURRENT", "3"))
        self.tool_queue_timeout = int(os.getenv("MCP_QUEUE_TIMEOUT", "60"))

        # OAuth 2.1 Configurations
        self.oauth_jwt_secret = os.getenv("OAUTH_JWT_SECRET", "pecorino-secret-key-change-in-prod")
        self.oauth_resource = os.getenv("OAUTH_RESOURCE", "pecorino://mcp-server")
        self.oauth_issuer = os.getenv("OAUTH_ISSUER", "https://auth.pecorino.com")
        self.oauth_required = os.getenv("OAUTH_REQUIRED", "true").lower() in ("true", "1", "yes")

        # Workspace and Index Storage Configurations
        workspace_root_env = os.getenv("PECORINO_WORKSPACE_ROOT")
        if workspace_root_env:
            self.workspace_root = Path(workspace_root_env).expanduser().resolve()
        else:
            self.workspace_root = Path(__file__).resolve().parent.parent.parent

        index_dir_env = os.getenv("PECORINO_INDEX_DIR")
        if index_dir_env:
            self.index_dir = Path(index_dir_env).expanduser().resolve()
        else:
            self.index_dir = Path("~/.pecorino/indexes").expanduser()

        self.enable_embeddings = os.getenv("PECORINO_ENABLE_EMBEDDINGS", "true").lower() in ("true", "1", "yes")
        self.embedding_model = os.getenv("PECORINO_EMBEDDING_MODEL", "Xenova/all-MiniLM-L6-v2")
        default_dim = "384"
        if "nomic" in self.embedding_model.lower():
            default_dim = "768"
        elif "bge-large" in self.embedding_model.lower():
            default_dim = "1024"
        self.embedding_dim = int(os.getenv("PECORINO_EMBEDDING_DIM", default_dim))
        self.enable_lsp = os.getenv("PECORINO_ENABLE_LSP", "true").lower() in ("true", "1", "yes")

        # Allowed external roots (allowlist model for allow_external=True)
        # Set via colon-separated absolute paths, e.g.:
        #   PECORINO_ALLOWED_EXTERNAL_DIRS=/home/user/repos:/opt/projects
        self.allowed_external_roots: set[Path] = set()
        env_roots = os.getenv("PECORINO_ALLOWED_EXTERNAL_DIRS", "")
        for r in env_roots.split(":"):
            if r.strip():
                self.allowed_external_roots.add(Path(r.strip()).expanduser().resolve())

# Global singleton configuration
settings = Config()
