import asyncio
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

sdk_path = workspace_root / "modules" / "python-sdk" / "src"
if str(sdk_path) not in sys.path:
    sys.path.insert(0, str(sdk_path))

import time

import jwt
from src.mcp_core.metrics import TOOL_CALLS, TOOL_ERRORS, TOOL_DURATION
import mcp.types as types
from mcp.server.lowlevel import Server, NotificationOptions
from mcp.server.models import InitializationOptions

from src.gitstats_ast import ClassDef, InterfaceDef, walk
from src.gitstats_export import MetricsExporter
from src.gitstats_gitdatacollector import GitDataCollector
from src.gitstats_hotspot import HotspotDetector
from src.gitstats_maintainability import (
    calculate_halstead_metrics,
    calculate_loc_metrics,
    calculate_maintainability_index,
    calculate_mccabe_complexity,
)
from src.gitstats_oopmetrics import OOPMetricsAnalyzer, parse

server = Server("OOP Metrics Analyzer Server 🚀")

SUPPORTED = {'.py','.pyi','.java','.scala','.kt','.js','.jsx','.ts','.tsx',
             '.cpp','.cc','.cxx','.c','.h','.hpp','.hxx','.go','.rs','.swift'}

def safe_path(p: str) -> Path:
    path = Path(p).expanduser().resolve()
    if not path.is_relative_to(workspace_root):
        raise ValueError(f"Path outside workspace: {path}")
    if not path.exists():
        raise ValueError(f"Not found: {path}")
    return path

# Core implementation of tools (without decorators)
async def do_browse(target: str, view: str = "summary", query: Optional[str] = None, limit: int = 10) -> dict:
    if view == "search":
        if not query:
            raise ValueError("Query is required for search view")
        from src.gitstats_index import CodeSearchIndex
        index = CodeSearchIndex()
        results = await asyncio.to_thread(index.search, query, limit)
        return {"query": query, "results": results}

    path = safe_path(target)
    from src.gitstats_index import CodeSearchIndex
    index = CodeSearchIndex()

    if path.is_dir():
        if view not in ["summary", "search"]:
            raise ValueError(f"View '{view}' is only supported for specific files, not directories.")

        nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        indexed_files = len(set(n['filepath'] for n in nodes))

        return {
            "target": str(path),
            "type": "directory",
            "structure": {
                "indexed_files": indexed_files,
                "total_files_found": indexed_files
            }
        }

    if path.suffix not in SUPPORTED:
        raise ValueError(f"Unsupported extension: {path.suffix}")

    result = {"target": str(path), "type": "file"}

    if view == "tree":
        import tree_sitter

        from src.gitstats_tree_sitter_parser import get_language_from_extension
        from src.tsgm import TreeSitterGrammarManager
        content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
        lang = TreeSitterGrammarManager().get_language(get_language_from_extension(path.suffix))
        parser = tree_sitter.Parser(lang)
        ts_tree = parser.parse(content.encode())
        result["structure"] = {"tree": str(ts_tree.root_node)}
    elif view == "deps":
        content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
        tree = await asyncio.to_thread(parse, content, path.suffix)
        result["structure"] = [{"module": i.module, "names": i.names} for i in tree.imports]
    else:
        nodes = await asyncio.to_thread(index.get_file_nodes, str(path))
        if view == "classes":
            result["structure"] = [
                {"name": n['name'], "line": n['start_line'], "metrics": n.get('metrics', {})}
                for n in nodes if n['node_type'] in ('class', 'interface')
            ]
        elif view == "functions":
            result["structure"] = [
                {"name": n['name'], "cc": n.get('metrics', {}).get('cyclomatic_complexity', 1), "line": n['start_line']}
                for n in nodes if n['node_type'] in ('function', 'method')
            ]
        else: # summary
            classes = sum(1 for n in nodes if n['node_type'] in ('class', 'interface'))
            functions = sum(1 for n in nodes if n['node_type'] in ('function', 'method'))
            result["structure"] = {
                "classes": classes,
                "functions": functions
            }

    return result

async def do_metrics(target: str, what: List[str] = ["all"]) -> dict:
    path = safe_path(target)
    what_set = {w.lower() for w in what}
    if "all" in what_set:
        what_set = {"oop", "complexity", "hotspots"}

    result = {"target": str(path), "type": "file" if path.is_file() else "directory"}

    if path.is_file():
        content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
        if "oop" in what_set or "complexity" in what_set:
            analyzer = OOPMetricsAnalyzer(use_ast=True)
            oop_res = await asyncio.to_thread(analyzer.analyze_file, str(path), content, path.suffix)
            if "oop" in what_set:
                result["oop"] = oop_res
            if "complexity" in what_set:
                loc = await asyncio.to_thread(calculate_loc_metrics, content, path.suffix)
                hal = await asyncio.to_thread(calculate_halstead_metrics, content, path.suffix)
                mcc = await asyncio.to_thread(calculate_mccabe_complexity, content, path.suffix)
                mi = await asyncio.to_thread(calculate_maintainability_index, loc, hal, mcc)
                result["complexity"] = {"loc": loc, "halstead": hal, "mccabe": mcc, "mi": mi}
    else:
        if "oop" in what_set or "complexity" in what_set:
            analyzer = OOPMetricsAnalyzer(use_ast=True)
            ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist"}
            files = [f for f in path.rglob("*") if f.suffix in SUPPORTED and not any(p in ignore_dirs for p in f.parts)]
            for fp in files:
                try:
                    txt = await asyncio.to_thread(fp.read_text, encoding='utf-8', errors='ignore')
                    analyzer.analyze_file(str(fp), txt, fp.suffix)
                except Exception:
                    pass
            analyzer.calculate_afferent_coupling()
            result["metrics_summary"] = analyzer.analyze_package(str(path))

    if "hotspots" in what_set and path.is_dir() and (path/".git").exists():
        data = GitDataCollector()
        await asyncio.to_thread(data.collect, str(path))
        await asyncio.to_thread(data.calculate_mi_for_repository, str(path))
        await asyncio.to_thread(data.refine)
        detector = HotspotDetector(data)
        hotspots = await asyncio.to_thread(detector.analyze)
        result["hotspots"] = hotspots[:30]

    return result

async def do_report(repo_path: str, output_path: str) -> dict:
    path = safe_path(repo_path)
    if not (path/".git").exists():
        raise ValueError(f"Not a git repository: {path}")

    import re
    repo_name = path.name
    safe_repo_name = re.sub(r'[<>:"/\\|?*]', '_', repo_name).strip('. ')
    if not safe_repo_name:
        safe_repo_name = 'unnamed_repo'

    out_dir = Path(output_path).expanduser().resolve() / f"{safe_repo_name}_report"
    out_dir.mkdir(parents=True, exist_ok=True)

    from src.gitstats_config import conf
    conf['calculate_mi_per_repository'] = True

    data = GitDataCollector()
    await asyncio.to_thread(data.collect, str(path))
    await asyncio.to_thread(data.calculate_mi_for_repository, str(path))
    await asyncio.to_thread(data.calculate_mccabe_for_repository, str(path))
    await asyncio.to_thread(data.calculate_halstead_for_repository, str(path))
    await asyncio.to_thread(data.calculate_oop_for_repository, str(path))
    await asyncio.to_thread(data.refine)

    detector = HotspotDetector(data)
    hotspots = await asyncio.to_thread(detector.analyze)
    summary = detector.get_summary()

    exporter = MetricsExporter(data, {"hotspots": hotspots, "summary": summary})
    json_file = await asyncio.to_thread(exporter.export_json, str(out_dir))

    return {
        "status": "success",
        "report_path": json_file
    }

async def do_update_index(target: str) -> dict:
    path = safe_path(target)
    from src.gitstats_index import CodeSearchIndex
    index = CodeSearchIndex()

    async def _index_file_tree(filepath: str, content: str, tree):
        await asyncio.to_thread(index.clear_file, filepath)
        content_lines = content.splitlines()
        nodes_to_index = []

        for node in walk(tree):
            if isinstance(node, ClassDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'class',
                    'filepath': filepath,
                    'body_text': body,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'metrics': {'wmc': getattr(node, 'wmc', 0), 'cbo': getattr(node, 'cbo', 0), 'rfc': getattr(node, 'rfc', 0), 'lcom': getattr(node, 'lcom', 0)}
                })
                for m in node.methods:
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'body_text': m_body,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': {'cyclomatic_complexity': getattr(m, 'cyclomatic_complexity', 1)}
                    })
            elif isinstance(node, InterfaceDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                nodes_to_index.append({
                    'name': node.name,
                    'node_type': 'interface',
                    'filepath': filepath,
                    'body_text': body,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'metrics': {}
                })
                for m in node.methods:
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    nodes_to_index.append({
                        'name': f"{node.name}.{m.name}",
                        'node_type': 'method',
                        'filepath': filepath,
                        'body_text': m_body,
                        'start_line': m.lineno,
                        'end_line': m.end_lineno,
                        'metrics': {'cyclomatic_complexity': getattr(m, 'cyclomatic_complexity', 1)}
                    })

        for func in tree.functions:
            f_body = '\n'.join(content_lines[max(0, func.lineno-1):func.end_lineno]) if func.lineno > 0 else ''
            nodes_to_index.append({
                'name': func.name,
                'node_type': 'function',
                'filepath': filepath,
                'body_text': f_body,
                'start_line': func.lineno,
                'end_line': func.end_lineno,
                'metrics': {'cyclomatic_complexity': getattr(func, 'cyclomatic_complexity', 1)}
            })

        if nodes_to_index:
            await asyncio.to_thread(index.index_nodes, nodes_to_index)

    if path.is_dir():
        ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist"}
        files = [f for f in path.rglob("*") if f.suffix in SUPPORTED and not any(p in ignore_dirs for p in f.parts)]
        indexed_count = 0
        for fp in files:
            try:
                content = await asyncio.to_thread(fp.read_text, encoding='utf-8', errors='ignore')
                tree = await asyncio.to_thread(parse, content, fp.suffix)
                await _index_file_tree(str(fp), content, tree)
                indexed_count += 1
            except Exception:
                pass
        return {"status": "success", "indexed_files": indexed_count, "total_files_found": len(files)}

    if path.suffix not in SUPPORTED:
        raise ValueError(f"Unsupported extension: {path.suffix}")

    content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
    tree = await asyncio.to_thread(parse, content, path.suffix)
    await _index_file_tree(str(path), content, tree)
    return {"status": "success", "indexed_files": 1, "total_files_found": 1}

# Low-level Handlers

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="browse",
            description="Browse the codebase (structure or semantic search). If view='search', requires query. For structure, view can be: summary, classes, functions, deps, tree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "view": {"type": "string", "default": "summary"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["target"]
            }
        ),
        types.Tool(
            name="metrics",
            description="Calculate OOP, complexity, or hotspot metrics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "what": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["all"]
                    }
                },
                "required": ["target"]
            }
        ),
        types.Tool(
            name="report",
            description="Generate and export a full analysis report to disk.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "output_path": {"type": "string"}
                },
                "required": ["repo_path", "output_path"]
            }
        ),
        types.Tool(
            name="update_index",
            description="Update the codebase SQLite index via AST browsing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"}
                },
                "required": ["target"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> types.CallToolResult:
    start_time = time.time()

    sys.stderr.write(f"[INFO] MCP Tool Call: '{name}' with arguments: {json.dumps(arguments)}\n")
    sys.stderr.flush()

    TOOL_CALLS.labels(tool=name).inc()

    try:
        if name == "browse":
            res = await do_browse(
                target=arguments.get("target"),
                view=arguments.get("view", "summary"),
                query=arguments.get("query"),
                limit=arguments.get("limit", 10)
            )
        elif name == "metrics":
            res = await do_metrics(
                target=arguments.get("target"),
                what=arguments.get("what", ["all"])
            )
        elif name == "report":
            res = await do_report(
                repo_path=arguments.get("repo_path"),
                output_path=arguments.get("output_path")
            )
        elif name == "update_index":
            res = await do_update_index(
                target=arguments.get("target")
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        duration = time.time() - start_time
        sys.stderr.write(f"[INFO] MCP Tool Success: '{name}' in {duration:.4f}s\n")
        sys.stderr.flush()

        TOOL_DURATION.labels(tool=name).observe(duration)

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(res, indent=2))]
        )
    except Exception as e:
        duration = time.time() - start_time
        sys.stderr.write(f"[ERROR] MCP Tool Failure: '{name}' after {duration:.4f}s - Error: {str(e)}\n")
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()

        TOOL_ERRORS.labels(tool=name).inc()

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error: {e}")],
            is_error=True
        )

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="browse",
            description="Browse codebase structure or search.",
            arguments=[
                types.PromptArgument(name="target", description="Target path to browse", required=True),
                types.PromptArgument(name="view", description="View type (summary, classes, etc.)", required=False)
            ]
        ),
        types.Prompt(
            name="metrics",
            description="Calculate OOP, complexity, or hotspot metrics.",
            arguments=[
                types.PromptArgument(name="target", description="Target path", required=True),
                types.PromptArgument(name="what", description="What to measure (oop, complexity, hotspots, all)", required=False)
            ]
        ),
        types.Prompt(
            name="report",
            description="Export a full analysis report to disk.",
            arguments=[
                types.PromptArgument(name="repo_path", description="Repository path", required=True),
                types.PromptArgument(name="output_path", description="Output JSON report path", required=True)
            ]
        ),
        types.Prompt(
            name="update_index",
            description="Update the codebase SQLite index via AST browsing.",
            arguments=[
                types.PromptArgument(name="target", description="Target path to index", required=True)
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = arguments or {}
    if name == "browse":
        target = args.get("target", "")
        view = args.get("view", "summary")
        return types.GetPromptResult(
            description="Browse codebase structure or search",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the browse tool on target '{target}' with view '{view}'."))]
        )
    elif name == "metrics":
        target = args.get("target", "")
        what = args.get("what", "all")
        return types.GetPromptResult(
            description="Calculate metrics",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please calculate '{what}' metrics for the target '{target}'."))]
        )
    elif name == "report":
        repo_path = args.get("repo_path", "")
        output_path = args.get("output_path", "")
        return types.GetPromptResult(
            description="Generate report",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please generate and export an analysis report for '{repo_path}' to '{output_path}'."))]
        )
    elif name == "update_index":
        target = args.get("target", "")
        return types.GetPromptResult(
            description="Update SQLite index",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the update_index tool on target '{target}'."))]
        )
    raise ValueError(f"Prompt not found: {name}")

@server.completion()
async def handle_completion(
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None = None
) -> types.Completion:
    if isinstance(ref, types.PromptReference):
        if ref.name == "browse":
            if argument.name == "view":
                views = ["summary", "classes", "functions", "deps", "tree", "search"]
                return types.Completion(
                    values=[v for v in views if v.startswith(argument.value.lower())],
                    hasMore=False
                )
            elif argument.name == "target":
                val = argument.value or ""
                matches = glob.glob(val + "*") + glob.glob(val + "**/*", recursive=True)
                files = []
                for m in matches:
                    if os.path.isfile(m):
                        ext = os.path.splitext(m)[1].lower()
                        if ext in SUPPORTED:
                            files.append(m)
                    if len(files) >= 50:
                        break
                return types.Completion(values=sorted(files)[:20], hasMore=len(files) > 20)

        elif ref.name == "metrics":
            if argument.name == "what":
                whats = ["oop", "complexity", "hotspots", "all"]
                return types.Completion(
                    values=[w for w in whats if w.startswith(argument.value.lower())],
                    hasMore=False
                )
            elif argument.name == "target":
                val = argument.value or ""
                matches = glob.glob(val + "*")
                paths = sorted(matches)[:20]
                return types.Completion(values=paths, hasMore=len(matches) > 20)

        elif ref.name == "report":
            if argument.name == "repo_path":
                val = argument.value or ""
                matches = [m for m in glob.glob(val + "*") if os.path.isdir(m)]
                paths = sorted(matches)[:20]
                return types.Completion(values=paths, hasMore=len(matches) > 20)

        elif ref.name == "update_index":
            if argument.name == "target":
                val = argument.value or ""
                matches = glob.glob(val + "*") + glob.glob(val + "**/*", recursive=True)
                files = []
                for m in matches:
                    if os.path.isfile(m):
                        ext = os.path.splitext(m)[1].lower()
                        if ext in SUPPORTED:
                            files.append(m)
                    if len(files) >= 50:
                        break
                return types.Completion(values=sorted(files)[:20], hasMore=len(files) > 20)

    return types.Completion(values=[], total=None, hasMore=None)


