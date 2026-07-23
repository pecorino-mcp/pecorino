from src.mcp_server.hcgs import build_levels, build_static_summary


def test_build_static_summary_basic():
    summary = build_static_summary(
        node_id="file.py::func::1",
        name="validate_token",
        kind="Function",
        signature="validate_token(token: str) -> bool",
        complexity=3
    )
    assert "Function validate_token." in summary
    assert "Signature: validate_token(token: str) -> bool" in summary
    assert "Complexity score: 3." in summary

def test_build_static_summary_with_child_context():
    called_summaries = [
        ("decode_jwt", "Function decode_jwt. Purpose: Decodes JSON Web Tokens."),
        ("check_expiry", "Function check_expiry. Purpose: Validates token expiration date.")
    ]
    summary = build_static_summary(
        node_id="auth.py::auth_user::10",
        name="auth_user",
        kind="Function",
        docstring="Authenticates user by verifying token.",
        called_summaries=called_summaries
    )
    assert "Function auth_user." in summary
    assert "Purpose: Authenticates user by verifying token." in summary
    assert "Calls: decode_jwt (Function decode_jwt), check_expiry (Function check_expiry)." in summary

def test_build_levels_empty_graph():
    class DummyGraph:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_val, exc_tb): return False
        def query(self, q): return []

    levels = build_levels(DummyGraph())
    assert levels == []
