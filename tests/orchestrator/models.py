"""Pydantic models for the orchestrator REST API."""

from typing import Any, Optional
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """Register a new tool/library as a worker."""
    name: str = Field(..., description="Unique worker name (e.g., 'gitstats', 'my-analyzer')")
    source: str = Field(..., description="Source path or git URL of the legacy codebase")
    adapter: Optional[str] = Field(
        None,
        description="Force a specific adapter ('python', 'node', 'gitstats'). Auto-detected if omitted."
    )


class RunRequest(BaseModel):
    """Execute a registered worker with a payload."""
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass to the tool (e.g., repo_path, mode, entry_point)"
    )
    network_mode: Optional[str] = Field(
        None,
        description="Override network mode for this run ('none', 'bridge', 'host')"
    )
    wait: bool = Field(
        True,
        description="If True, block until the worker completes and return results. If False, return immediately with worker_id."
    )
    timeout: int = Field(
        300,
        description="Max seconds to wait for completion (only if wait=True)"
    )


class WorkerInfo(BaseModel):
    """Metadata about a registered worker."""
    name: str
    source: str
    adapter: str
    base_image: str
    status: str  # "registered", "running", "completed", "error"
    container_id: Optional[str] = None
    output_dir: Optional[str] = None


class WorkerStatus(BaseModel):
    """Runtime status of a worker."""
    worker_id: str
    status: str
    container_id: Optional[str] = None
    adapter: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    exit_code: Optional[int] = None
    output: Optional[Any] = None
    error: Optional[str] = None
