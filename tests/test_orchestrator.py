"""
Integration tests for the On-Demand Worker Orchestrator.

Tests cover:
  1. Language auto-detection (Gitstats → Python → Node)
  2. Adapter interface contracts
  3. Config auto-creation
  4. API endpoint validation (using FastAPI TestClient)
"""

import os
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.orchestrator.config import OrchestratorConfig
from tests.orchestrator.adapters import detect_adapter, list_adapters, get_adapter_by_name
from tests.orchestrator.adapters.base import BaseWorkerAdapter
from tests.orchestrator.adapters.python_adapter import PythonAdapter
from tests.orchestrator.adapters.node_adapter import NodeAdapter
from tests.orchestrator.adapters.gitstats_adapter import GitstatsAdapter


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace for testing."""
    return tmp_path


@pytest.fixture
def python_project(tmp_workspace):
    """Create a minimal Python project structure."""
    project_dir = tmp_workspace / "python-tool"
    project_dir.mkdir()
    (project_dir / "requirements.txt").write_text("requests>=2.0\npyyaml\n")
    (project_dir / "main.py").write_text(
        'import json\nprint(json.dumps({"status": "ok", "tool": "python-test"}))\n'
    )
    return str(project_dir)


@pytest.fixture
def node_project(tmp_workspace):
    """Create a minimal Node.js project structure."""
    project_dir = tmp_workspace / "node-tool"
    project_dir.mkdir()
    (project_dir / "package.json").write_text(json.dumps({
        "name": "test-node-tool",
        "version": "1.0.0",
        "main": "index.js",
        "scripts": {"start": "node index.js"},
    }))
    (project_dir / "index.js").write_text(
        'console.log(JSON.stringify({status: "ok", tool: "node-test"}));\n'
    )
    return str(project_dir)


@pytest.fixture
def gitstats_project():
    """Use the actual gitstats3 project as the test source."""
    project_root = Path(__file__).resolve().parent.parent
    return str(project_root)


# ──────────────────────────────────────────────────────────
# Test: Config Auto-Creation
# ──────────────────────────────────────────────────────────

class TestConfig:
    def test_auto_creates_directories(self, tmp_workspace):
        """Config should auto-create all cache subdirectories."""
        config = OrchestratorConfig(base_dir=tmp_workspace / "orchestrator_test")

        assert config.cache_dir.exists()
        assert (config.cache_dir / "pip").exists()
        assert (config.cache_dir / "npm").exists()
        assert (config.cache_dir / "maven").exists()
        assert config.workspace_dir.exists()
        assert config.output_dir.exists()

    def test_default_network_mode_is_none(self):
        """Security: network should be 'none' by default."""
        config = OrchestratorConfig()
        assert config.network_mode == "none"

    def test_default_images_populated(self):
        """Should have default images for python, node, java."""
        config = OrchestratorConfig()
        assert "python" in config.default_images
        assert "node" in config.default_images
        assert "java" in config.default_images


# ──────────────────────────────────────────────────────────
# Test: Language Auto-Detection
# ──────────────────────────────────────────────────────────

class TestLanguageDetection:
    def test_detects_python_from_requirements(self, python_project):
        """Should detect Python adapter from requirements.txt."""
        adapter = detect_adapter(python_project)
        assert adapter.name == "python"

    def test_detects_node_from_package_json(self, node_project):
        """Should detect Node adapter from package.json."""
        adapter = detect_adapter(node_project)
        assert adapter.name == "node"

    def test_detects_gitstats(self, gitstats_project):
        """Should detect Gitstats adapter from gitstats.py or pyproject.toml."""
        adapter = detect_adapter(gitstats_project)
        assert adapter.name == "gitstats"

    def test_detection_priority_gitstats_over_python(self, gitstats_project):
        """Gitstats should be detected before generic Python (higher priority)."""
        adapter = detect_adapter(gitstats_project)
        # Gitstats project has both pyproject.toml AND gitstats.py
        # Auto-detect should pick gitstats (more specific) over generic python
        assert adapter.name == "gitstats"

    def test_fallback_to_python(self, tmp_workspace):
        """Unknown project should fall back to Python adapter."""
        empty_dir = tmp_workspace / "unknown"
        empty_dir.mkdir()
        adapter = detect_adapter(str(empty_dir))
        assert adapter.name == "python"

    def test_get_adapter_by_name(self):
        """Should retrieve adapters by name."""
        assert get_adapter_by_name("python").name == "python"
        assert get_adapter_by_name("node").name == "node"
        assert get_adapter_by_name("gitstats").name == "gitstats"

    def test_get_adapter_by_name_invalid(self):
        """Should raise ValueError for unknown adapter names."""
        with pytest.raises(ValueError, match="No adapter registered"):
            get_adapter_by_name("cobol")


# ──────────────────────────────────────────────────────────
# Test: Adapter Interface Contracts
# ──────────────────────────────────────────────────────────

class TestAdapterContracts:
    def test_python_adapter_base_image(self):
        """Python adapter should return a Python base image."""
        adapter = PythonAdapter()
        assert "python" in adapter.get_base_image()

    def test_node_adapter_base_image(self):
        """Node adapter should return a Node base image."""
        adapter = NodeAdapter()
        assert "node" in adapter.get_base_image()

    def test_gitstats_adapter_extends_python(self):
        """Gitstats adapter should inherit from PythonAdapter."""
        adapter = GitstatsAdapter()
        assert isinstance(adapter, PythonAdapter)

    def test_python_install_uses_cache(self):
        """Python install command should use /cache/pip for offline-first."""
        adapter = PythonAdapter()
        cmd = adapter.get_install_command()
        assert "/cache/pip" in cmd

    def test_node_install_uses_cache(self):
        """Node install command should use /cache/npm for offline-first."""
        adapter = NodeAdapter()
        cmd = adapter.get_install_command()
        assert "/cache/npm" in cmd

    def test_entrypoint_script_has_phases(self):
        """Entrypoint script should have install and run phases."""
        adapter = PythonAdapter()
        script = adapter.get_entrypoint_script({"entry_point": "main.py"})
        assert "install" in script
        assert "run" in script
        assert "/output" in script

    def test_gitstats_run_modes(self):
        """Gitstats adapter should support report, metrics, and index modes."""
        adapter = GitstatsAdapter()

        report_cmd = adapter.get_run_command({"mode": "report"})
        assert "gitstats.py" in report_cmd

        metrics_cmd = adapter.get_run_command({"mode": "metrics"})
        assert "do_metrics" in metrics_cmd

        index_cmd = adapter.get_run_command({"mode": "index"})
        assert "do_update_index" in index_cmd


# ──────────────────────────────────────────────────────────
# Test: Adapter Registry
# ──────────────────────────────────────────────────────────

class TestAdapterRegistry:
    def test_list_adapters_returns_all(self):
        """Should list all registered adapters with metadata."""
        adapters = list_adapters()
        names = [a["name"] for a in adapters]
        assert "gitstats" in names
        assert "python" in names
        assert "node" in names

    def test_list_adapters_includes_base_image(self):
        """Each adapter listing should include its base image."""
        adapters = list_adapters()
        for a in adapters:
            assert "base_image" in a
            assert len(a["base_image"]) > 0


# ──────────────────────────────────────────────────────────
# Test: API Endpoints (FastAPI TestClient — no Docker needed)
# ──────────────────────────────────────────────────────────

class TestAPI:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        """Create a test client and clear registry before each test."""
        from tests.orchestrator.main import app, _registry
        _registry.clear()
        self.client = TestClient(app)

    def test_list_workers_empty(self):
        """GET /workers should return empty list initially."""
        resp = self.client.get("/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_adapters_endpoint(self):
        """GET /adapters should return available adapters."""
        resp = self.client.get("/adapters")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert "python" in names
        assert "node" in names

    def test_register_local_python_project(self, python_project):
        """POST /workers/register should register a local Python project."""
        resp = self.client.post("/workers/register", json={
            "name": "test-python",
            "source": python_project,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-python"
        assert data["adapter"] == "python"
        assert data["status"] == "registered"

    def test_register_duplicate_name_fails(self, python_project):
        """Registering the same name twice should return 409."""
        self.client.post("/workers/register", json={
            "name": "dup-test",
            "source": python_project,
        })
        resp = self.client.post("/workers/register", json={
            "name": "dup-test",
            "source": python_project,
        })
        assert resp.status_code == 409

    def test_register_invalid_source_fails(self):
        """Registering with a nonexistent path should return 400."""
        resp = self.client.post("/workers/register", json={
            "name": "bad-source",
            "source": "/nonexistent/path/to/nowhere",
        })
        assert resp.status_code == 400

    def test_status_unregistered_worker_404(self):
        """GET /workers/{name}/status for unknown worker should return 404."""
        resp = self.client.get("/workers/ghost/status")
        assert resp.status_code == 404

    def test_deregister_worker(self, python_project):
        """DELETE /workers/{name} should remove a registered worker."""
        self.client.post("/workers/register", json={
            "name": "to-delete",
            "source": python_project,
        })
        resp = self.client.delete("/workers/to-delete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deregistered"

        # Should be gone now
        resp = self.client.get("/workers/to-delete/status")
        assert resp.status_code == 404
