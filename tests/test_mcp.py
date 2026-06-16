import asyncio
import io
import sys
from pathlib import Path
import pytest

from src.gitstats_index import find_repo_root, get_db_path_for_repo
from src.mcp_core.server import do_browse, do_update_index


def test_repo_root_resolution():
    workspace = Path(__file__).resolve().parent.parent
    repo_root = find_repo_root(str(workspace / "src" / "gitstats_mcp.py"))
    assert Path(repo_root).resolve() == workspace.resolve()

    db_path = get_db_path_for_repo(repo_root)
    assert db_path.endswith("_code_search.db")


@pytest.mark.asyncio
async def test_do_update_and_browse():
    workspace = Path(__file__).resolve().parent.parent
    target_file = str(workspace / "src" / "gitstats_mcp.py")

    # Update index for the file
    res_update = await do_update_index(target_file)
    assert res_update["status"] == "success"
    assert res_update["indexed_files"] == 1

    # Browse the file for functions
    res_browse = await do_browse(target_file, view="functions")
    assert res_browse["target"] == target_file
    assert res_browse["type"] == "file"
    assert len(res_browse["structure"]) > 0
    assert res_browse["structure"][0]["name"] == "main"

    # Browse the file for summary, verifying graph metrics are present
    res_summary = await do_browse(target_file, view="summary")
    assert res_summary["target"] == target_file
    assert "graph_metrics" in res_summary["structure"]
    
    graph_metrics = res_summary["structure"]["graph_metrics"]
    assert "incoming_dependencies" in graph_metrics
    assert "outgoing_dependencies" in graph_metrics
    assert "pagerank_score" in graph_metrics

    # Browse the file for deps, verifying graph metrics
    res_deps = await do_browse(target_file, view="deps")
    assert res_deps["target"] == target_file
    assert "graph_metrics" in res_deps
    assert "outgoing_dependencies" in res_deps["graph_metrics"]

