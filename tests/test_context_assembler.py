"""Tests for Phase 6: Context Assembly."""
from src.mcp_server.context_assembler import assemble_context, to_gorgonzola_id


class DummyGraph:
    def query(self, query: str, params: dict = None):
        params = params or {}
        _ = params.get("id")

        if "CALLS" in query and "(caller)" in query:
            # Callers query
            return [{"id": "foo", "name": "caller_foo", "filepath": "a.py", "line": 10}]
        if "CALLS" in query and "(callee)" in query:
            # Callees query
            return [{"id": "bar", "name": "callee_bar", "filepath": "b.py", "line": 20}]
        if "DEFINES" in query:
            # Parent query
            return [{"name": "MyClass", "kind": "class"}]
        return []

class TestContextAssembler:
    def test_to_gorgonzola_id(self):
        r = {"id": "fallback", "filepath": "src/foo.py", "name": "bar", "kind": "function"}
        assert to_gorgonzola_id(r) == "src/foo.py::bar"

        r2 = {"id": "fallback", "filepath": "src/foo.py", "name": "MyClass.my_method", "kind": "method"}
        assert to_gorgonzola_id(r2) == "src/foo.py::MyClass::my_method"

        r3 = {"id": "fallback"}
        assert to_gorgonzola_id(r3) == "fallback"

    def test_assemble_context_graph(self):
        graph = DummyGraph()
        result = {"id": "test_id", "filepath": "test.py", "name": "test_fn", "kind": "function"}

        # Test without git (workspace_root = "")
        ctx = assemble_context(result, graph, "")

        assert "callers" in ctx
        assert ctx["callers"][0]["name"] == "caller_foo"

        assert "callees" in ctx
        assert ctx["callees"][0]["name"] == "callee_bar"

        assert "parent" in ctx
        assert ctx["parent"]["name"] == "MyClass"
        assert ctx["parent"]["kind"] == "class"

    def test_assemble_context_git(self, tmp_path, monkeypatch):
        # We can mock subprocess.check_output to avoid actual git calls
        import subprocess
        def mock_check_output(cmd, **kwargs):
            return b"hash123|feat: added GH-123 and #456|Alice|2023-01-01\nhash456|fix: PROJ-99|Bob|2023-01-02"

        monkeypatch.setattr(subprocess, "check_output", mock_check_output)

        # Create dummy file so os.path.exists passes
        test_file = tmp_path / "test.py"
        test_file.touch()

        result = {"id": "test_id", "filepath": "test.py", "name": "test_fn"}
        ctx = assemble_context(result, None, str(tmp_path))

        assert "recent_commits" in ctx
        assert len(ctx["recent_commits"]) == 2
        assert ctx["recent_commits"][0]["hash"] == "hash123"
        assert ctx["recent_commits"][0]["author"] == "Alice"

        assert "related_issues" in ctx
        issues = ctx["related_issues"]
        assert len(issues) == 3
        assert set(issues) == {"GH-123", "#456", "PROJ-99"}
