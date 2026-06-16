import os
import json
import pytest
from src.gitstats_mcp import inspect_code_structure, calculate_metrics, analyze_repository, tree_sitter_cli


class MockProgress:
    async def set_total(self, total: int):
        pass
    async def set_message(self, message: str):
        pass
    async def increment(self):
        pass

@pytest.mark.asyncio
async def test_inspect_code_structure():
    target = "src/tsgm.py"
    
    # Test full AST view
    full_res = await inspect_code_structure(target, view="full")
    data = json.loads(full_res)
    assert "error" not in data
    assert "type" in data
    
    # Test classes view
    classes_res = await inspect_code_structure(target, view="classes")
    classes = json.loads(classes_res)
    assert isinstance(classes, list)
    assert len(classes) == 1
    assert classes[0]["name"] == "TreeSitterGrammarManager"
    assert classes[0]["type"] == "class"
    
    # Test functions view
    funcs_res = await inspect_code_structure(target, view="functions")
    funcs = json.loads(funcs_res)
    assert isinstance(funcs, list)
    func_names = [f["name"] for f in funcs]
    assert "__init__" in func_names
    assert "install" in func_names
    
    # Test dependencies view
    deps_res = await inspect_code_structure(target, view="dependencies")
    deps = json.loads(deps_res)
    assert isinstance(deps, list)
    dep_modules = [d["module"] for d in deps]
    assert "importlib" in dep_modules
    assert "tree_sitter" in dep_modules

    # Test tree_sitter view
    ts_res = await inspect_code_structure(target, view="tree_sitter")
    ts_data = json.loads(ts_res)
    assert "error" not in ts_data
    assert "stdout" in ts_data
    assert "module" in ts_data["stdout"]

    # Test that inspect_code_structure saves/indexes to DB
    from src.gitstats_index import CodeSearchIndex
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_path = os.path.join(workspace_dir, "code_search.db")
    index = CodeSearchIndex(db_path)
    
    # Search the DB for TreeSitterGrammarManager
    results = index.search("TreeSitterGrammarManager")
    assert len(results) > 0
    abs_target = os.path.abspath(target)
    assert any(r['filepath'] == abs_target for r in results)

@pytest.mark.asyncio
async def test_mcp_resources():
    from src.gitstats_mcp import mcp
    
    # Test version resource
    version_res = await mcp.read_resource("gitstats://version")
    assert json.loads(list(version_res)[0].content) == {"version": "3.0.0"}

    # Test inspect resource (via template)
    inspect_res = await mcp.read_resource("gitstats://inspect/src%2Ftsgm.py")
    inspect_data = json.loads(list(inspect_res)[0].content)
    assert "error" not in inspect_data
    assert "type" in inspect_data

    # Test tree_sitter resource (via template)
    ts_res = await mcp.read_resource("gitstats://tree_sitter/src%2Ftsgm.py")
    ts_data = json.loads(list(ts_res)[0].content)
    assert "error" not in ts_data
    assert "stdout" in ts_data

    # Test search resource (via template)
    search_res = await mcp.read_resource("gitstats://search/grammar")
    search_data = json.loads(list(search_res)[0].content)
    assert "error" not in search_data
    assert "results" in search_data


@pytest.mark.asyncio
async def test_calculate_metrics():
    target_file = "src/tsgm.py"
    file_res = await calculate_metrics(target_file, scope="file")
    metrics = json.loads(file_res)
    
    assert "error" not in metrics
    assert "filepath" in metrics
    assert "classes_defined" in metrics
    assert "complexity_metrics" in metrics
    assert "loc" in metrics["complexity_metrics"]
    assert "mccabe" in metrics["complexity_metrics"]
    
    package_res = await calculate_metrics("src/scripts", scope="package")
    pkg_metrics = json.loads(package_res)
    assert "error" not in pkg_metrics
    assert "file_count" in pkg_metrics
    assert pkg_metrics["file_count"] >= 1

@pytest.mark.asyncio
async def test_analyze_repository():
    res = await analyze_repository(".", action="analyze")
    analysis = json.loads(res)
    assert "error" not in analysis
    assert isinstance(analysis, dict)

@pytest.mark.asyncio
async def test_tree_sitter_cli():
    res = await tree_sitter_cli(["--version"])
    data = json.loads(res)
    assert "error" not in data
    assert "stdout" in data
    assert "tree-sitter 0.26.9" in data["stdout"]


class MockElicitResult:
    def __init__(self, action, data):
        self.action = action
        self.data = data


class MockElicitContext:
    def __init__(self, action, path_value):
        self.action = action
        self.path_value = path_value

    async def elicit(self, message, schema):
        fields = schema.model_fields if hasattr(schema, "model_fields") else {}
        if "filepath" in fields:
            data = schema(filepath=self.path_value)
        else:
            data = schema(dirpath=self.path_value)
        return MockElicitResult(self.action, data)


@pytest.mark.asyncio
async def test_mcp_completions():
    from src.gitstats_mcp import handle_completion
    from mcp.types import PromptReference, CompletionArgument, ResourceTemplateReference

    # Test prompt view parameter completion
    ref = PromptReference(name="inspect_code_structure_prompt")
    arg = CompletionArgument(name="view", value="fun")
    res = await handle_completion(ref, arg, None)
    assert res is not None
    assert "functions" in res.values

    # Test prompt filepath completion
    arg = CompletionArgument(name="filepath", value="src/tsg")
    res = await handle_completion(ref, arg, None)
    assert res is not None
    assert any("tsgm.py" in val for val in res.values)

    # Test prompt scope completion
    ref = PromptReference(name="calculate_metrics_prompt")
    arg = CompletionArgument(name="scope", value="pa")
    res = await handle_completion(ref, arg, None)
    assert res is not None
    assert "package" in res.values


@pytest.mark.asyncio
async def test_mcp_elicitation():
    from src.gitstats_mcp import _elicit_filepath, _elicit_directory

    # Valid file requires no elicitation
    path = await _elicit_filepath("src/tsgm.py", ctx=None)
    assert "tsgm.py" in path

    # Invalid file with elicitation fallback
    mock_ctx = MockElicitContext("accept", "src/tsgm.py")
    path = await _elicit_filepath("invalid_file_path.py", ctx=mock_ctx)
    assert "tsgm.py" in path

    # Invalid file with elicitation decline
    mock_ctx = MockElicitContext("decline", "src/tsgm.py")
    with pytest.raises(ValueError):
        await _elicit_filepath("invalid_file_path.py", ctx=mock_ctx)

