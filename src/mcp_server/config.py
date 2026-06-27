import os
import json
from pathlib import Path

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
        
        # External directories configurations
        self._config_dir = Path("~/.pecorino").expanduser()
        self._config_file = self._config_dir / "config.json"
        self.allowed_external_dirs = set()
        
        # 1. Load from environment variable PECORINO_ALLOWED_EXTERNAL_DIRS
        env_dirs = os.getenv("PECORINO_ALLOWED_EXTERNAL_DIRS", "")
        if env_dirs:
            for d in env_dirs.split(os.pathsep):
                if d.strip():
                    try:
                        self.allowed_external_dirs.add(Path(d.strip()).expanduser().resolve())
                    except Exception:
                        pass
                        
        # 2. Load from JSON config file
        self._load_from_json()

    def _load_from_json(self):
        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    dirs = data.get("allowed_external_dirs", [])
                    for d in dirs:
                        try:
                            self.allowed_external_dirs.add(Path(d).expanduser().resolve())
                        except Exception:
                            pass
            except Exception:
                pass

    def _save_to_json(self):
        self._config_dir.mkdir(exist_ok=True)
        try:
            dirs_to_save = sorted(list(str(d) for d in self.allowed_external_dirs))
            data = {"allowed_external_dirs": dirs_to_save}
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def add_external_dir(self, path_str: str) -> str:
        try:
            p = Path(path_str).expanduser().resolve()
            if not p.exists():
                raise ValueError(f"Path does not exist: {path_str}")
            self.allowed_external_dirs.add(p)
            self._save_to_json()
            # Dynamically update the core allowed workspace roots to avoid circular import issues
            from src.mcp_server.core import register_allowed_root
            register_allowed_root(p)
            return str(p)
        except Exception as e:
            raise ValueError(f"Failed to add external directory: {e}")

    def remove_external_dir(self, path_str: str) -> str:
        try:
            p = Path(path_str).expanduser().resolve()
            if p in self.allowed_external_dirs:
                self.allowed_external_dirs.remove(p)
                self._save_to_json()
                from src.mcp_server.core import unregister_allowed_root
                unregister_allowed_root(p)
                return str(p)
            else:
                for d in list(self.allowed_external_dirs):
                    if str(d) == path_str or d.as_posix() == path_str:
                        self.allowed_external_dirs.remove(d)
                        self._save_to_json()
                        from src.mcp_server.core import unregister_allowed_root
                        unregister_allowed_root(d)
                        return str(d)
                raise ValueError(f"Path not found in allowed external directories: {path_str}")
        except Exception as e:
            raise ValueError(f"Failed to remove external directory: {e}")

    def list_external_dirs(self) -> list[str]:
        return sorted(list(str(d) for d in self.allowed_external_dirs))

# Global singleton configuration
settings = Config()
