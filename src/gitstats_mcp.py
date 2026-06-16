import os
import sys
import json
import asyncio
import glob
from typing import List, Dict, Any, Optional, Set
from pathlib import Path

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

sdk_path = workspace_root / "python-sdk-1.27.2" / "src"
if str(sdk_path) not in sys.path:
    sys.path.insert(0, str(sdk_path))

from pydantic import BaseModel, Field

from src.gitstats_oopmetrics import OOPMetricsAnalyzer, parse
from src.gitstats_ast import walk, ClassDef, InterfaceDef, FunctionDef
from src.gitstats_maintainability import (
    calculate_loc_metrics, calculate_halstead_metrics,
    calculate_mccabe_complexity, calculate_maintainability_index
)
from src.gitstats_hotspot import HotspotDetector
from src.gitstats_gitdatacollector import GitDataCollector
from src.gitstats_export import MetricsExporter

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

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

    async def _index_file_tree(filepath: str, content: str, tree):
        await asyncio.to_thread(index.clear_file, filepath)
        content_lines = content.splitlines()
        for node in walk(tree):
            if isinstance(node, ClassDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                await asyncio.to_thread(
                    index.index_node,
                    name=node.name,
                    node_type='class',
                    filepath=filepath,
                    body_text=body,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    metrics={'wmc': getattr(node, 'wmc', 0), 'cbo': getattr(node, 'cbo', 0), 'rfc': getattr(node, 'rfc', 0), 'lcom': getattr(node, 'lcom', 0)}
                )
                for m in node.methods:
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    await asyncio.to_thread(
                        index.index_node,
                        name=f"{node.name}.{m.name}",
                        node_type='method',
                        filepath=filepath,
                        body_text=m_body,
                        start_line=m.lineno,
                        end_line=m.end_lineno,
                        metrics={'cyclomatic_complexity': getattr(m, 'cyclomatic_complexity', 1)}
                    )
            elif isinstance(node, InterfaceDef):
                body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
                await asyncio.to_thread(
                    index.index_node,
                    name=node.name,
                    node_type='interface',
                    filepath=filepath,
                    body_text=body,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    metrics={}
                )
                for m in node.methods:
                    m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
                    await asyncio.to_thread(
                        index.index_node,
                        name=f"{node.name}.{m.name}",
                        node_type='method',
                        filepath=filepath,
                        body_text=m_body,
                        start_line=m.lineno,
                        end_line=m.end_lineno,
                        metrics={'cyclomatic_complexity': getattr(m, 'cyclomatic_complexity', 1)}
                    )
        
        for func in tree.functions:
            f_body = '\n'.join(content_lines[max(0, func.lineno-1):func.end_lineno]) if func.lineno > 0 else ''
            await asyncio.to_thread(
                index.index_node,
                name=func.name,
                node_type='function',
                filepath=filepath,
                body_text=f_body,
                start_line=func.lineno,
                end_line=func.end_lineno,
                metrics={'cyclomatic_complexity': getattr(func, 'cyclomatic_complexity', 1)}
            )

    if path.is_dir():
        if view not in ["summary", "search"]:
            raise ValueError(f"View '{view}' is only supported for specific files, not directories.")
        files = [f for f in path.rglob("*") if f.suffix in SUPPORTED and ".git" not in f.parts]
        indexed_count = 0
        for fp in files:
            try:
                content = await asyncio.to_thread(fp.read_text, encoding='utf-8', errors='ignore')
                tree = await asyncio.to_thread(parse, content, fp.suffix)
                await _index_file_tree(str(fp), content, tree)
                indexed_count += 1
            except Exception:
                pass
        return {
            "target": str(path),
            "type": "directory",
            "structure": {
                "indexed_files": indexed_count,
                "total_files_found": len(files)
            }
        }
        
    if path.suffix not in SUPPORTED:
        raise ValueError(f"Unsupported extension: {path.suffix}")

    content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
    tree = await asyncio.to_thread(parse, content, path.suffix)
    
    try:
        await _index_file_tree(str(path), content, tree)
    except Exception as db_err:
        import sys
        print(f"Warning: Failed to index inspected code to DB: {db_err}", file=sys.stderr)
        
    result = {"target": str(path), "type": "file"}
    
    if view == "tree":
        from src.gitstats_tree_sitter_parser import get_language_from_extension
        from src.tsgm import TreeSitterGrammarManager
        import tree_sitter
        lang = TreeSitterGrammarManager().get_language(get_language_from_extension(path.suffix))
        parser = tree_sitter.Parser(lang)
        ts_tree = parser.parse(content.encode())
        result["structure"] = {"tree": str(ts_tree.root_node)}
    elif view == "classes":
        result["structure"] = [
            {"name": n.name, "bases": getattr(n, 'bases', []), "methods": len(n.methods), "line": n.lineno}
            for n in walk(tree) if isinstance(n, (ClassDef, InterfaceDef))
        ]
    elif view == "functions":
        funcs = [{"name": f.name, "args": f.args, "cc": getattr(f, 'cyclomatic_complexity', 1), "line": f.lineno}
                 for f in tree.functions]
        funcs += [{"name": m.name, "class": c.name, "cc": getattr(m, 'cyclomatic_complexity', 1)}
                  for c in walk(tree) if isinstance(c, ClassDef) for m in c.methods]
        result["structure"] = funcs
    elif view == "deps":
        result["structure"] = [{"module": i.module, "names": i.names} for i in tree.imports]
    else: # summary
        result["structure"] = {
            "classes": sum(1 for n in walk(tree) if isinstance(n, ClassDef)),
            "functions": len(tree.functions),
            "imports": len(tree.imports)
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
            files = [f for f in path.rglob("*") if f.suffix in SUPPORTED and ".git" not in f.parts]
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
        
    out_dir = Path(output_path).expanduser().resolve()
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
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    arguments = arguments or {}
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
        else:
            raise ValueError(f"Unknown tool: {name}")
            
        return [types.TextContent(type="text", text=json.dumps(res, indent=2))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]

@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri="gitstats://version",
            name="GitStats Version",
            description="Returns the current version of the GitStats MCP Server",
            mimeType="application/json"
        ),
        types.Resource(
            uri="gitstats://summary",
            name="GitStats Summary",
            description="Returns summary info (deprecated)",
            mimeType="application/json"
        )
    ]

@server.list_resource_templates()
async def handle_list_resource_templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            uriTemplate="gitstats://browse/{filepath}",
            name="Browse Codebase",
            description="Resource to browse the structure of a source file.",
            mimeType="application/json"
        ),
        types.ResourceTemplate(
            uriTemplate="gitstats://metrics/{filepath}",
            name="File/Directory Metrics",
            description="Resource to calculate metrics for a file or directory.",
            mimeType="application/json"
        ),
        types.ResourceTemplate(
            uriTemplate="gitstats://search/{query}",
            name="Semantic Code Search",
            description="Resource to run semantic code search.",
            mimeType="application/json"
        )
    ]

@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    if uri == "gitstats://version":
        return json.dumps({"version": "3.0.0"})
    elif uri == "gitstats://summary":
        return json.dumps({"error": "Global state removed. Please use the 'metrics' tool directly."}, indent=2)
    elif uri.startswith("gitstats://browse/"):
        filepath = uri.replace("gitstats://browse/", "")
        res = await do_browse(target=filepath, view="summary")
        return json.dumps(res, indent=2)
    elif uri.startswith("gitstats://metrics/"):
        filepath = uri.replace("gitstats://metrics/", "")
        res = await do_metrics(target=filepath, what=["all"])
        return json.dumps(res, indent=2)
    elif uri.startswith("gitstats://search/"):
        query = uri.replace("gitstats://search/", "")
        res = await do_browse(target=str(workspace_root), view="search", query=query)
        return json.dumps(res, indent=2)
    raise ValueError(f"Resource not found: {uri}")

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="browse_prompt",
            description="Browse codebase structure or search.",
            arguments=[
                types.PromptArgument(name="target", description="Target path to browse", required=True),
                types.PromptArgument(name="view", description="View type (summary, classes, etc.)", required=False)
            ]
        ),
        types.Prompt(
            name="metrics_prompt",
            description="Calculate OOP, complexity, or hotspot metrics.",
            arguments=[
                types.PromptArgument(name="target", description="Target path", required=True),
                types.PromptArgument(name="what", description="What to measure (oop, complexity, hotspots, all)", required=False)
            ]
        ),
        types.Prompt(
            name="report_prompt",
            description="Export a full analysis report to disk.",
            arguments=[
                types.PromptArgument(name="repo_path", description="Repository path", required=True),
                types.PromptArgument(name="output_path", description="Output JSON report path", required=True)
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    arguments = arguments or {}
    if name == "browse_prompt":
        target = arguments.get("target", "")
        view = arguments.get("view", "summary")
        return types.GetPromptResult(
            description="Browse codebase structure or search",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the browse tool on target '{target}' with view '{view}'."))]
        )
    elif name == "metrics_prompt":
        target = arguments.get("target", "")
        what = arguments.get("what", "all")
        return types.GetPromptResult(
            description="Calculate metrics",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please calculate '{what}' metrics for the target '{target}'."))]
        )
    elif name == "report_prompt":
        repo_path = arguments.get("repo_path", "")
        output_path = arguments.get("output_path", "")
        return types.GetPromptResult(
            description="Generate report",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please generate and export an analysis report for '{repo_path}' to '{output_path}'."))]
        )
    raise ValueError(f"Prompt not found: {name}")

@server.completion()
async def handle_completion(
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None,
) -> types.Completion | None:
    if isinstance(ref, types.PromptReference):
        if ref.name == "browse_prompt":
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
                
        elif ref.name == "metrics_prompt":
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
                
        elif ref.name == "report_prompt":
            if argument.name == "repo_path":
                val = argument.value or ""
                matches = [m for m in glob.glob(val + "*") if os.path.isdir(m)]
                paths = sorted(matches)[:20]
                return types.Completion(values=paths, hasMore=len(matches) > 20)

    if isinstance(ref, types.ResourceTemplateReference):
        if ref.uri in ["gitstats://browse/{filepath}", "gitstats://metrics/{filepath}"]:
            if argument.name == "filepath":
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
    return None

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
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from mcp.server.sse import SseServerTransport
    
    transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with transport.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
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

    async def handle_messages(request):
        await transport.handle_post_message(request.scope, request.receive, request._send)

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=handle_messages, methods=["POST"]),
    ])
    
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
            from starlette.applications import Starlette
        except ImportError:
            print("Error: starlette and uvicorn must be installed to use SSE transport.", file=sys.stderr)
            sys.exit(1)
        asyncio.run(run_sse(args.host, args.port))
    elif args.transport == "streamable-http":
        print("streamable-http is not currently supported directly without custom routing.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
