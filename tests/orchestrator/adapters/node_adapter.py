import os
import json

from tests.orchestrator.adapters.base import BaseWorkerAdapter
from tests.orchestrator.config import settings


class NodeAdapter(BaseWorkerAdapter):
    """Adapter for Node.js/JavaScript codebases. Detects package.json."""

    @property
    def name(self) -> str:
        return "node"

    def detect(self, source_path: str) -> bool:
        return os.path.exists(os.path.join(source_path, "package.json"))

    def get_base_image(self) -> str:
        return settings.default_images.get("node", "node:18-slim")

    def get_install_command(self) -> str:
        # Offline-first: use npm cache mounted from host
        return "npm install --prefer-offline --cache /cache/npm --no-audit --no-fund 2>/dev/null"

    def get_run_command(self, payload: dict) -> str:
        entry = payload.get("entry_point")
        if entry:
            return f"node {entry} > /output/result.json 2>/output/stderr.log"

        # Auto-detect from package.json "main" or "scripts.start"
        script = payload.get("script", "start")
        return (
            f"if npm run {script} --if-present > /output/result.json 2>/output/stderr.log; then "
            "true; "
            "elif [ -f index.js ]; then "
            "node index.js > /output/result.json 2>/output/stderr.log; "
            "else "
            "echo '{\"error\": \"No entry_point found\"}' > /output/result.json; "
            "fi"
        )
