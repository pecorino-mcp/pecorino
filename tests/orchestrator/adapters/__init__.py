"""
Adapter registry with auto-detection.
Detection order: Gitstats → Python → Node → fallback Python.
"""

from tests.orchestrator.adapters.base import BaseWorkerAdapter
from tests.orchestrator.adapters.gitstats_adapter import GitstatsAdapter
from tests.orchestrator.adapters.python_adapter import PythonAdapter
from tests.orchestrator.adapters.node_adapter import NodeAdapter

# Ordered by specificity: most specific first, generic last
_ADAPTER_REGISTRY: list[BaseWorkerAdapter] = [
    GitstatsAdapter(),
    PythonAdapter(),
    NodeAdapter(),
]

# Fallback adapter when nothing else matches
_FALLBACK = PythonAdapter()


def detect_adapter(source_path: str) -> BaseWorkerAdapter:
    """
    Auto-detect the best adapter for a given source codebase.

    Tries each registered adapter in order (Gitstats → Python → Node).
    Falls back to PythonAdapter if nothing matches.

    Args:
        source_path: Absolute path to the cloned/local source code directory.

    Returns:
        The first matching adapter instance.
    """
    for adapter in _ADAPTER_REGISTRY:
        if adapter.detect(source_path):
            return adapter
    return _FALLBACK


def get_adapter_by_name(name: str) -> BaseWorkerAdapter:
    """Look up an adapter by its name. Raises ValueError if not found."""
    for adapter in _ADAPTER_REGISTRY:
        if adapter.name == name:
            return adapter
    raise ValueError(f"No adapter registered with name: {name}")


def list_adapters() -> list[dict]:
    """Return metadata about all registered adapters."""
    return [{"name": a.name, "base_image": a.get_base_image()} for a in _ADAPTER_REGISTRY]
