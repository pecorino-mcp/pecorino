"""
Docker Runtime Engine.

Manages the full container lifecycle using the Docker Python SDK.
All Docker interactions are isolated here — swapping to K8s later
means replacing this class with a K8sRuntime implementing the same interface.
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

import docker
from docker.errors import NotFound, APIError

from tests.orchestrator.adapters.base import BaseWorkerAdapter
from tests.orchestrator.config import settings


class DockerRuntime:
    """
    Manages ephemeral Docker containers for on-demand workers.

    Design:
        - Source code mounted READ-ONLY (:ro) — legacy code is never modified
        - Cache directory mounted READ-WRITE — populated on first run, reused later
        - Output directory mounted READ-WRITE — worker writes JSON results here
        - Containers run with --rm semantics (auto-cleaned on exit)
        - Network mode defaults to 'none' (no outbound access) for security
    """

    def __init__(self):
        self.client = docker.from_env()
        self._active_containers: dict[str, dict] = {}

    def spawn(
        self,
        adapter: BaseWorkerAdapter,
        source_path: str,
        payload: dict,
        worker_id: str,
        network_mode: Optional[str] = None,
    ) -> str:
        """
        Spawn an ephemeral container to run a legacy tool.

        Args:
            adapter: The language/tool adapter describing how to run the code.
            source_path: Absolute host path to the legacy source code.
            payload: User-provided arguments to pass to the tool.
            worker_id: Unique identifier for this worker run.
            network_mode: Override network mode (default from config).

        Returns:
            Docker container ID.
        """
        net_mode = network_mode or settings.network_mode
        output_dir = settings.output_dir / worker_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate the entrypoint script
        entrypoint_script = adapter.get_entrypoint_script(payload)

        # Write entrypoint to a temp file on the host
        entrypoint_dir = settings.workspace_dir / worker_id
        entrypoint_dir.mkdir(parents=True, exist_ok=True)
        entrypoint_file = entrypoint_dir / "entrypoint.sh"
        entrypoint_file.write_text(entrypoint_script)
        entrypoint_file.chmod(0o755)

        # Resolve cache subdirectory based on adapter language
        cache_subdir = settings.cache_dir / self._cache_key(adapter)

        # Volume mounts
        volumes = {
            str(Path(source_path).resolve()): {"bind": "/app", "mode": "ro"},
            str(cache_subdir): {"bind": "/cache", "mode": "rw"},
            str(output_dir): {"bind": "/output", "mode": "rw"},
            str(entrypoint_file): {"bind": "/run/entrypoint.sh", "mode": "ro"},
        }

        image = adapter.get_base_image()

        # Ensure base image is available locally
        self._ensure_image(image)

        try:
            container = self.client.containers.run(
                image=image,
                command=["sh", "/run/entrypoint.sh"],
                volumes=volumes,
                network_mode=net_mode,
                detach=True,
                name=f"worker-{worker_id}",
                working_dir="/app",
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,  # 50% of 1 CPU
            )

            self._active_containers[worker_id] = {
                "container_id": container.id,
                "adapter": adapter.name,
                "source_path": source_path,
                "output_dir": str(output_dir),
                "started_at": time.time(),
            }

            return container.id

        except APIError as e:
            sys.stderr.write(f"[ERROR] Failed to spawn container: {e}\n")
            raise

    def status(self, worker_id: str) -> dict:
        """Check the current state of a worker container."""
        info = self._active_containers.get(worker_id)
        if not info:
            return {"worker_id": worker_id, "status": "not_found"}

        try:
            container = self.client.containers.get(info["container_id"])
            container.reload()

            result = {
                "worker_id": worker_id,
                "container_id": info["container_id"][:12],
                "status": container.status,
                "adapter": info["adapter"],
                "started_at": info["started_at"],
                "elapsed_seconds": round(time.time() - info["started_at"], 1),
            }

            # If exited, try to read the result file
            if container.status == "exited":
                result_file = Path(info["output_dir"]) / "result.json"
                if result_file.exists():
                    try:
                        result["output"] = json.loads(result_file.read_text())
                    except Exception:
                        result["output"] = result_file.read_text()

            return result

        except NotFound:
            return {"worker_id": worker_id, "status": "destroyed"}

    def logs(self, worker_id: str, tail: int = 100) -> str:
        """Get stdout/stderr logs from a worker container."""
        info = self._active_containers.get(worker_id)
        if not info:
            return ""

        try:
            container = self.client.containers.get(info["container_id"])
            return container.logs(tail=tail).decode("utf-8", errors="replace")
        except NotFound:
            return ""

    def get_result(self, worker_id: str) -> Optional[dict]:
        """Read the JSON result from a completed worker."""
        info = self._active_containers.get(worker_id)
        if not info:
            return None

        result_file = Path(info["output_dir"]) / "result.json"
        if not result_file.exists():
            return None

        try:
            return json.loads(result_file.read_text())
        except Exception:
            return {"raw_output": result_file.read_text()}

    def kill(self, worker_id: str) -> dict:
        """Force stop and remove a worker container, clean up workspace."""
        info = self._active_containers.get(worker_id)
        if not info:
            return {"worker_id": worker_id, "status": "not_found"}

        try:
            container = self.client.containers.get(info["container_id"])
            container.stop(timeout=5)
            container.remove(force=True)
        except NotFound:
            pass  # Already gone
        except APIError as e:
            sys.stderr.write(f"[WARNING] Error removing container: {e}\n")

        # Clean up entrypoint workspace (not source code, not output)
        entrypoint_dir = settings.workspace_dir / worker_id
        if entrypoint_dir.exists():
            import shutil
            shutil.rmtree(entrypoint_dir, ignore_errors=True)

        self._active_containers.pop(worker_id, None)

        return {
            "worker_id": worker_id,
            "status": "destroyed",
            "output_dir": info["output_dir"],
        }

    def wait(self, worker_id: str, timeout: int = 300) -> dict:
        """
        Block until a worker container exits or timeout is reached.
        Returns the final status including output.
        """
        info = self._active_containers.get(worker_id)
        if not info:
            return {"worker_id": worker_id, "status": "not_found"}

        try:
            container = self.client.containers.get(info["container_id"])
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)

            status = self.status(worker_id)
            status["exit_code"] = exit_code
            return status

        except Exception as e:
            return {"worker_id": worker_id, "status": "error", "error": str(e)}

    def _ensure_image(self, image: str):
        """Pull the base image if not available locally."""
        try:
            self.client.images.get(image)
        except docker.errors.ImageNotFound:
            sys.stderr.write(f"[INFO] Pulling base image: {image} (first-time only)...\n")
            self.client.images.pull(image)
            sys.stderr.write(f"[INFO] Image {image} pulled successfully.\n")

    def _cache_key(self, adapter: BaseWorkerAdapter) -> str:
        """Map adapter to cache subdirectory name."""
        name = adapter.name
        if name in ("python", "gitstats"):
            return "pip"
        elif name == "node":
            return "npm"
        else:
            return name
