"""
On-Demand Worker Orchestrator — Control Plane API.

A lightweight FastAPI service that manages the lifecycle of ephemeral
Docker containers running legacy tool/library codebases.

Usage:
    uvicorn orchestrator.main:app --port 9000
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

from tests.orchestrator.adapters import detect_adapter, get_adapter_by_name, list_adapters
from tests.orchestrator.config import settings
from tests.orchestrator.models import RegisterRequest, RunRequest, WorkerInfo, WorkerStatus
from tests.orchestrator.runtime.docker_runtime import DockerRuntime

app = FastAPI(
    title="On-Demand Worker Orchestrator",
    description="Turn any legacy library into an ephemeral, on-demand worker. "
                "Language-agnostic. Zero refactor. Local enterprise.",
    version="0.1.0",
)

runtime = DockerRuntime()

# In-memory registry of registered workers
# In production, this would be backed by a persistent store (SQLite, Redis, etc.)
_registry: dict[str, dict] = {}


# ──────────────────────────────────────────────────────────
# Worker Registration
# ──────────────────────────────────────────────────────────

@app.post("/workers/register", response_model=WorkerInfo)
def register_worker(req: RegisterRequest):
    """
    Register a legacy tool/library as an on-demand worker.

    If source is a git URL, it will be cloned into the workspace.
    If source is a local path, it will be used directly.
    The adapter (Python/Node/Gitstats) is auto-detected from the source code.
    """
    if req.name in _registry:
        raise HTTPException(status_code=409, detail=f"Worker '{req.name}' already registered")

    # Resolve source: clone if git URL, otherwise use local path
    source_path = _resolve_source(req.name, req.source)

    # Detect or force adapter
    if req.adapter:
        try:
            adapter = get_adapter_by_name(req.adapter)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown adapter: {req.adapter}")
    else:
        adapter = detect_adapter(source_path)

    _registry[req.name] = {
        "name": req.name,
        "source": req.source,
        "source_path": source_path,
        "adapter_name": adapter.name,
        "status": "registered",
        "container_id": None,
        "worker_run_id": None,
    }

    return WorkerInfo(
        name=req.name,
        source=req.source,
        adapter=adapter.name,
        base_image=adapter.get_base_image(),
        status="registered",
    )


# ──────────────────────────────────────────────────────────
# Worker Lifecycle: Start / Run / Stop
# ──────────────────────────────────────────────────────────

@app.post("/workers/{name}/run")
def run_worker(name: str, req: RunRequest):
    """
    Execute a registered worker.

    This is the "1-click" endpoint: it spawns the container, runs the tool,
    collects results, and optionally waits for completion.
    """
    worker = _get_worker(name)
    adapter = get_adapter_by_name(worker["adapter_name"])

    # Generate a unique run ID
    run_id = f"{name}-{uuid.uuid4().hex[:8]}"

    # Spawn container
    try:
        container_id = runtime.spawn(
            adapter=adapter,
            source_path=worker["source_path"],
            payload=req.payload,
            worker_id=run_id,
            network_mode=req.network_mode,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn container: {e}")

    worker["status"] = "running"
    worker["container_id"] = container_id[:12]
    worker["worker_run_id"] = run_id

    if req.wait:
        # Block until done
        result = runtime.wait(run_id, timeout=req.timeout)

        # Auto-cleanup container
        runtime.kill(run_id)
        worker["status"] = "completed"
        worker["container_id"] = None

        return result
    else:
        return {
            "worker_id": run_id,
            "container_id": container_id[:12],
            "status": "running",
            "message": f"Worker spawned. Poll GET /workers/{name}/status for results.",
        }


@app.post("/workers/{name}/stop")
def stop_worker(name: str):
    """Force stop a running worker and clean up its container."""
    worker = _get_worker(name)

    run_id = worker.get("worker_run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="No active run to stop")

    result = runtime.kill(run_id)
    worker["status"] = "stopped"
    worker["container_id"] = None
    worker["worker_run_id"] = None

    return result


@app.get("/workers/{name}/status")
def worker_status(name: str):
    """Check the current status of a worker, including output if completed."""
    worker = _get_worker(name)

    run_id = worker.get("worker_run_id")
    if not run_id:
        return WorkerInfo(
            name=worker["name"],
            source=worker["source"],
            adapter=worker["adapter_name"],
            base_image=get_adapter_by_name(worker["adapter_name"]).get_base_image(),
            status=worker["status"],
        )

    return runtime.status(run_id)


@app.get("/workers/{name}/logs")
def worker_logs(name: str, tail: int = 100):
    """Get stdout/stderr logs from a running or completed worker."""
    worker = _get_worker(name)
    run_id = worker.get("worker_run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="No active run")

    return {"worker_id": run_id, "logs": runtime.logs(run_id, tail=tail)}


# ──────────────────────────────────────────────────────────
# Registry Management
# ──────────────────────────────────────────────────────────

@app.get("/workers")
def list_workers():
    """List all registered workers."""
    result = []
    for name, w in _registry.items():
        adapter = get_adapter_by_name(w["adapter_name"])
        result.append(WorkerInfo(
            name=w["name"],
            source=w["source"],
            adapter=w["adapter_name"],
            base_image=adapter.get_base_image(),
            status=w["status"],
            container_id=w.get("container_id"),
        ))
    return result


@app.delete("/workers/{name}")
def deregister_worker(name: str):
    """Remove a worker from the registry. Stops any running container first."""
    worker = _get_worker(name)

    # Stop if running
    run_id = worker.get("worker_run_id")
    if run_id:
        runtime.kill(run_id)

    # Clean up cloned source if it's in our workspace
    source_workspace = settings.workspace_dir / name
    if source_workspace.exists():
        shutil.rmtree(source_workspace, ignore_errors=True)

    _registry.pop(name, None)
    return {"status": "deregistered", "name": name}


@app.get("/adapters")
def list_available_adapters():
    """List all available language adapters."""
    return list_adapters()


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _get_worker(name: str) -> dict:
    """Look up a registered worker or raise 404."""
    if name not in _registry:
        raise HTTPException(status_code=404, detail=f"Worker '{name}' not registered")
    return _registry[name]


def _resolve_source(name: str, source: str) -> str:
    """
    Resolve the source to a local directory path.
    If it's a git URL, clone it into the workspace.
    If it's a local path, validate and return it.
    """
    # Check if it looks like a git URL
    if source.startswith("http://") or source.startswith("https://") or source.startswith("git@"):
        clone_dir = settings.workspace_dir / name
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", source, str(clone_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to clone {source}: {e.stderr}"
            )

        return str(clone_dir)

    # Local path
    try:
        source_path = Path(source).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid source path")

    # Validate resolved path is within allowed directories to prevent path injection
    project_root = Path(__file__).resolve().parent.parent.parent
    allowed_roots = [
        project_root,
        settings.base_dir.resolve(),
        Path("/tmp").resolve()
    ]

    if not any(source_path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(
            status_code=400,
            detail="Access denied: local source path must reside within workspace, project root, or /tmp"
        )

    if not source_path.exists():
        raise HTTPException(status_code=400, detail=f"Source path does not exist: {source}")

    return str(source_path)
