import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class OrchestratorConfig:
    """
    Configuration for the On-Demand Worker Orchestrator.
    All paths auto-created on first access. All values overridable via env vars.
    """

    # Root directory for all orchestrator data
    base_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("ORCHESTRATOR_BASE_DIR", str(Path.home() / ".orchestrator"))
    ))

    # Where source code gets cloned/copied
    workspace_dir: Path = field(default=None)

    # Where worker outputs (JSON results) are written
    output_dir: Path = field(default=None)

    # Shared dependency cache mounted into every container
    cache_dir: Path = field(default=None)

    # Docker network mode: "none" (secure default) or "bridge" (if tool needs network)
    network_mode: str = field(default_factory=lambda: os.environ.get(
        "ORCHESTRATOR_NETWORK_MODE", "none"
    ))

    # Auto-stop idle containers after this many seconds (0 = no auto-stop)
    auto_stop_seconds: int = field(default_factory=lambda: int(os.environ.get(
        "ORCHESTRATOR_AUTO_STOP_SECONDS", "300"
    )))

    # Default Docker images per language (overridable)
    default_images: dict = field(default_factory=lambda: {
        "python": os.environ.get("ORCHESTRATOR_PYTHON_IMAGE", "python:3.10-slim"),
        "node": os.environ.get("ORCHESTRATOR_NODE_IMAGE", "node:18-slim"),
        "java": os.environ.get("ORCHESTRATOR_JAVA_IMAGE", "openjdk:17-slim"),
    })

    def __post_init__(self):
        """Resolve derived paths and auto-create all directories."""
        if self.workspace_dir is None:
            self.workspace_dir = Path(os.environ.get(
                "ORCHESTRATOR_WORKSPACE_DIR", str(self.base_dir / "workspaces")
            ))
        if self.output_dir is None:
            self.output_dir = Path(os.environ.get(
                "ORCHESTRATOR_OUTPUT_DIR", str(self.base_dir / "outputs")
            ))
        if self.cache_dir is None:
            self.cache_dir = Path(os.environ.get(
                "ORCHESTRATOR_CACHE_DIR", str(self.base_dir / "caches")
            ))

        # Auto-create all required directories
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create directory tree on first launch. Idempotent."""
        dirs = [
            self.workspace_dir,
            self.output_dir,
            self.cache_dir / "pip",
            self.cache_dir / "npm",
            self.cache_dir / "maven",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# Singleton config instance
settings = OrchestratorConfig()
