import os

from tests.orchestrator.adapters.base import BaseWorkerAdapter
from tests.orchestrator.config import settings


class PythonAdapter(BaseWorkerAdapter):
    """Adapter for Python codebases. Detects requirements.txt / pyproject.toml / setup.py."""

    @property
    def name(self) -> str:
        return "python"

    def detect(self, source_path: str) -> bool:
        markers = ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"]
        return any(os.path.exists(os.path.join(source_path, m)) for m in markers)

    def get_base_image(self) -> str:
        return settings.default_images.get("python", "python:3.10-slim")

    def get_install_command(self) -> str:
        # Offline-first: try local cache, fall back to network (if network_mode allows)
        # The /cache/pip directory is mounted from the host's persistent cache
        return (
            "if [ -f requirements.txt ]; then "
            "pip install --cache-dir=/cache/pip --quiet -r requirements.txt; "
            "elif [ -f pyproject.toml ]; then "
            "pip install --cache-dir=/cache/pip --quiet .; "
            "elif [ -f setup.py ]; then "
            "pip install --cache-dir=/cache/pip --quiet .; "
            "fi"
        )

    def get_run_command(self, payload: dict) -> str:
        # Try to auto-detect the entry point
        entry = payload.get("entry_point")
        if entry:
            return f"python {entry} > /output/result.json 2>/output/stderr.log"

        # Auto-detect: check pyproject.toml [project.scripts], then fall back to main.py
        return (
            "if [ -f main.py ]; then "
            "python main.py > /output/result.json 2>/output/stderr.log; "
            "elif python -c \"import tomllib; "
            "t=tomllib.load(open('pyproject.toml','rb')); "
            "scripts=t.get('project',{}).get('scripts',{}); "
            "print(list(scripts.values())[0] if scripts else '')\" 2>/dev/null | grep -q '.'; then "
            "python -m $(python -c \"import tomllib; "
            "t=tomllib.load(open('pyproject.toml','rb')); "
            "s=t.get('project',{}).get('scripts',{}); "
            "print(list(s.keys())[0])\") > /output/result.json 2>/output/stderr.log; "
            "else "
            "echo '{\"error\": \"No entry_point found\"}' > /output/result.json; "
            "fi"
        )
