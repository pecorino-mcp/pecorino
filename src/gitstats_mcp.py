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
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from fastapi import Request, HTTPException, Response
from fastapi.responses import JSONResponse

# OAuth 2.1 Configurations
OAUTH_JWT_SECRET = os.getenv("OAUTH_JWT_SECRET", "gitstats3-secret-key-change-in-prod")
OAUTH_RESOURCE = os.getenv("OAUTH_RESOURCE", "gitstats3://mcp-server")
OAUTH_ISSUER = os.getenv("OAUTH_ISSUER", "https://auth.gitstats3.com")
OAUTH_REQUIRED = os.getenv("OAUTH_REQUIRED", "true").lower() in ("true", "1", "yes")

# Prometheus Metrics
TOOL_CALLS = Counter('mcp_tool_calls_total', 'Total number of MCP tool calls', ['tool'])
TOOL_ERRORS = Counter('mcp_tool_errors_total', 'Total number of MCP tool errors', ['tool'])
TOOL_DURATION = Histogram('mcp_tool_duration_seconds', 'Duration of MCP tool execution', ['tool'])
ACTIVE_SESSIONS = Gauge('mcp_active_sessions', 'Current active SSE sessions')

def verify_oauth_token(request: Request) -> dict:
    """
    Validates OAuth 2.1 token with resource indicators.
    Returns the decoded token claims if successful, or raises an HTTPException.
    """
    if not OAUTH_REQUIRED:
        return {"sub": "anonymous", "resource": OAUTH_RESOURCE}

    auth_header = request.headers.get("authorization")
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
    else:
        # Fallback to query params for SSE EventSource GET requests
        token = None
        if "query_string" in request.scope:
            try:
                token = request.query_params.get("token") or request.query_params.get("access_token")
            except (KeyError, ValueError):
                pass

    if not token:
        sys.stderr.write("[AUTH ERROR] Missing authorization token\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401, 
            detail="Bearer token required",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Bearer token required"'}
        )

    try:
        payload = jwt.decode(
            token,
            OAUTH_JWT_SECRET,
            algorithms=["HS256", "RS256"],
            options={"require": ["exp"], "verify_aud": False}
        )
    except jwt.ExpiredSignatureError as e:
        sys.stderr.write(f"[AUTH ERROR] Token expired: {e}\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401, 
            detail="Token has expired",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Token has expired"'}
        )
    except jwt.InvalidTokenError as e:
        sys.stderr.write(f"[AUTH ERROR] Invalid token: {e}\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401, 
            detail="Invalid token signature or format",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Invalid token signature or format"'}
        )

    if OAUTH_ISSUER and payload.get("iss") != OAUTH_ISSUER:
        sys.stderr.write(f"[AUTH ERROR] Issuer mismatch: got {payload.get('iss')}, expected {OAUTH_ISSUER}\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401, 
            detail="Issuer mismatch",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Issuer mismatch"'}
        )

    # Verify Resource Indicator (RFC 8707)
    resources = payload.get("resource") or payload.get("aud") or []
    if isinstance(resources, str):
        resources = [resources]

    if OAUTH_RESOURCE not in resources:
        sys.stderr.write(f"[AUTH ERROR] Resource mismatch: expected {OAUTH_RESOURCE} in {resources}\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=403, 
            detail="Invalid resource indicator (resource mismatch)",
            headers={"www-authenticate": 'Bearer error="invalid_target", error_description="Invalid resource indicator (resource mismatch)"'}
        )

    return payload

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

async def handle_list_tools(
    ctx: Any,
    params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="browse",
                description="Browse the codebase (structure or semantic search). If view='search', requires query. For structure, view can be: summary, classes, functions, deps, tree.",
                input_schema={
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
                input_schema={
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
                input_schema={
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
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"}
                    },
                    "required": ["target"]
                }
            )
        ]
    )

async def handle_call_tool(
    ctx: Any,
    params: types.CallToolRequestParams
) -> types.CallToolResult:
    name = params.name
    arguments = params.arguments or {}
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

async def handle_list_prompts(
    ctx: Any,
    params: types.PaginatedRequestParams | None
) -> types.ListPromptsResult:
    return types.ListPromptsResult(
        prompts=[
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
    )

async def handle_get_prompt(
    ctx: Any,
    params: types.GetPromptRequestParams
) -> types.GetPromptResult:
    name = params.name
    arguments = params.arguments or {}
    if name == "browse":
        target = arguments.get("target", "")
        view = arguments.get("view", "summary")
        return types.GetPromptResult(
            description="Browse codebase structure or search",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the browse tool on target '{target}' with view '{view}'."))]
        )
    elif name == "metrics":
        target = arguments.get("target", "")
        what = arguments.get("what", "all")
        return types.GetPromptResult(
            description="Calculate metrics",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please calculate '{what}' metrics for the target '{target}'."))]
        )
    elif name == "report":
        repo_path = arguments.get("repo_path", "")
        output_path = arguments.get("output_path", "")
        return types.GetPromptResult(
            description="Generate report",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please generate and export an analysis report for '{repo_path}' to '{output_path}'."))]
        )
    elif name == "update_index":
        target = arguments.get("target", "")
        return types.GetPromptResult(
            description="Update SQLite index",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the update_index tool on target '{target}'."))]
        )
    raise ValueError(f"Prompt not found: {name}")

async def handle_completion(
    ctx: Any,
    params: types.CompleteRequestParams
) -> types.CompleteResult:
    ref = params.ref
    argument = params.argument
    result = None
    if isinstance(ref, types.PromptReference):
        if ref.name == "browse":
            if argument.name == "view":
                views = ["summary", "classes", "functions", "deps", "tree", "search"]
                result = types.Completion(
                    values=[v for v in views if v.startswith(argument.value.lower())],
                    has_more=False
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
                result = types.Completion(values=sorted(files)[:20], has_more=len(files) > 20)

        elif ref.name == "metrics":
            if argument.name == "what":
                whats = ["oop", "complexity", "hotspots", "all"]
                result = types.Completion(
                    values=[w for w in whats if w.startswith(argument.value.lower())],
                    has_more=False
                )
            elif argument.name == "target":
                val = argument.value or ""
                matches = glob.glob(val + "*")
                paths = sorted(matches)[:20]
                result = types.Completion(values=paths, has_more=len(matches) > 20)

        elif ref.name == "report":
            if argument.name == "repo_path":
                val = argument.value or ""
                matches = [m for m in glob.glob(val + "*") if os.path.isdir(m)]
                paths = sorted(matches)[:20]
                result = types.Completion(values=paths, has_more=len(matches) > 20)

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
                result = types.Completion(values=sorted(files)[:20], has_more=len(files) > 20)

    return types.CompleteResult(
        completion=result if result is not None else types.Completion(values=[], total=None, has_more=None)
    )

# Register request handlers explicitly
server.add_request_handler("tools/list", types.PaginatedRequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)
server.add_request_handler("prompts/list", types.PaginatedRequestParams, handle_list_prompts)
server.add_request_handler("prompts/get", types.GetPromptRequestParams, handle_get_prompt)
server.add_request_handler("completion/complete", types.CompleteRequestParams, handle_completion)



async def run_stdio():
    import mcp.server.stdio
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="gitstats3",
                server_version="3.0.0",
                capabilities=server.get_capabilities(
                    notification_options=mcp.server.lowlevel.NotificationOptions(),
                    experimental_capabilities={},
                ),
            )
        )

async def run_sse(host: str, port: int):
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, JSONResponse
    from fastapi.exceptions import HTTPException

    transport = SseServerTransport("/messages/")
    app = FastAPI(title="gitstats3 MCP Server (SSE)")

    @app.middleware("http")
    async def oauth_middleware(request: Request, call_next):
        try:
            verify_oauth_token(request)
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"error": "invalid_token", "error_description": e.detail},
                headers=e.headers
            )
        return await call_next(request)

    async def handle_sse(request: Request):
        ACTIVE_SESSIONS.inc()
        try:
            async with transport.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="gitstats3",
                        server_version="3.0.0",
                        capabilities=server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    )
                )
        finally:
            ACTIVE_SESSIONS.dec()
        return Response()

    @app.get("/metrics")
    async def handle_metrics(request: Request):
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    app.add_route("/sse", handle_sse, methods=["GET"])
    app.mount("/messages/", transport.handle_post_message)

    config = uvicorn.Config(app, host=host, port=port)
    server_uv = uvicorn.Server(config)
    await server_uv.serve()

async def run_streamable_http(host: str, port: int):
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, JSONResponse
    from fastapi.exceptions import HTTPException

    app = FastAPI(title="gitstats3 MCP Server (Streamable HTTP)")

    @app.middleware("http")
    async def oauth_middleware(request: Request, call_next):
        try:
            verify_oauth_token(request)
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"error": "invalid_token", "error_description": e.detail},
                headers=e.headers
            )
        return await call_next(request)

    @app.get("/metrics")
    async def handle_metrics(request: Request):
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    streamable_app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True
    )

    app.mount("/mcp", streamable_app)

    config = uvicorn.Config(app, host=host, port=port)
    server_uv = uvicorn.Server(config)
    await server_uv.serve()

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run OOP Metrics Analyzer Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio", help="Transport protocol")
    parser.add_argument("--host", default="127.0.0.1", help="Host address for network transports")
    parser.add_argument("--port", type=int, default=8000, help="Port number for network transports")

    args, unknown = parser.parse_known_args()

    if args.transport == "stdio":
        asyncio.run(run_stdio())
    elif args.transport == "sse":
        try:
            import uvicorn
            import fastapi
        except ImportError:
            print("Error: fastapi and uvicorn must be installed to use SSE transport.", file=sys.stderr)
            sys.exit(1)
        asyncio.run(run_sse(args.host, args.port))
    elif args.transport == "streamable-http":
        try:
            import uvicorn
            import fastapi
        except ImportError:
            print("Error: fastapi and uvicorn must be installed to use streamable-http transport.", file=sys.stderr)
            sys.exit(1)
        asyncio.run(run_streamable_http(args.host, args.port))

if __name__ == "__main__":
    main()
