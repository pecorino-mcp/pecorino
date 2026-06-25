import asyncio
import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import mcp.types as types
from mcp.server.lowlevel import Server

from src.core.gitdatacollector import GitDataCollector
from src.mcp_server.metrics import TOOL_CALLS, TOOL_DURATION, TOOL_ERRORS
from src.metrics.hotspot import HotspotDetector
from src.metrics.maintainability import (
    calculate_halstead_metrics,
    calculate_loc_metrics,
    calculate_maintainability_index,
    calculate_mccabe_complexity,
)
from src.metrics.oopmetrics import OOPMetricsAnalyzer, parse
from src.utils.export import MetricsExporter

server = Server("OOP Metrics Analyzer Server 🚀")

SUPPORTED = {'.py','.pyi','.java','.scala','.kt','.js','.jsx','.ts','.tsx',
             '.cpp','.cc','.cxx','.c','.h','.hpp','.hxx','.go','.rs','.swift'}

# Add the workspace root (parent of 'src') to sys.path so we can import via 'src.xyz' package namespace
workspace_root = Path(__file__).resolve().parent.parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

def safe_path(p: str) -> Path:
    if not p:
        p = "."
    path = Path(p).expanduser().resolve()

    allowed_roots = [
        workspace_root,
        workspace_root.parent,
        workspace_root.parent.parent
    ]

    # Safely allow the current working directory, avoiding high-risk roots
    cwd = Path.cwd()
    restricted_roots = {Path("/"), Path.home()}

    if cwd not in restricted_roots:
        allowed_roots.append(cwd)
        # Also allow cwd.parent if it's not a restricted root
        if cwd.parent not in restricted_roots:
            allowed_roots.append(cwd.parent)

    if not any(path.is_relative_to(r) for r in allowed_roots):
        raise ValueError(f"Path outside workspace: {path}")
    if not path.exists():
        raise ValueError(f"Not found: {path}")
    return path

# Core implementation of tools (without decorators)
async def do_browse(target: str, view: str = "summary", query: Optional[str] = None, limit: int = 10, max_depth: int = 3, output_file: Optional[str] = None) -> dict:
    path = safe_path(target)
    from src.mcp_server.index import CodeSearchIndex, find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)
    index = CodeSearchIndex(db_path=db_path)

    # Initialize GraphAPI if view is a graph-related view
    api = None
    if view in ("callers", "callees", "impact", "summary", "deps"):
        from src.mcp_server.graph_api import GraphAPI
        api = GraphAPI(repo_path=repo_root)

    if view == "callers":
        if not query:
            raise ValueError("Query (function/method name) is required for callers view")
        callers = await asyncio.to_thread(api.find_callers, query)
        return {"status": "success", "target": query, "callers": callers}

    if view == "callees":
        if not query:
            raise ValueError("Query (function/method name) is required for callees view")
        callees = await asyncio.to_thread(api.find_callees, query)
        return {"status": "success", "target": query, "callees": callees}

    if view == "impact":
        deps = await asyncio.to_thread(api.impact_analysis, path.as_posix(), max_depth)
        return {"status": "success", "target": path.as_posix(), "dependent_files": deps}

    if view == "search":
        if not query:
            raise ValueError("Query is required for search view")
        results = await asyncio.to_thread(index.search, query, limit)
        return {"query": query, "results": results}


    if path.is_dir():
        if view not in ["summary", "search", "callers", "callees"]:
            raise ValueError(f"View '{view}' is only supported for specific files, not directories.")

        nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        indexed_files = len(set(n['filepath'] for n in nodes))


        return {
            "target": path.as_posix(),
            "type": "directory",
            "structure": {
                "indexed_files": indexed_files,
                "total_files_found": indexed_files
            }
        }

    if path.suffix not in SUPPORTED:
        raise ValueError(f"Unsupported extension: {path.suffix}")

    result: dict[str, Any] = {"target": path.as_posix(), "type": "file"}

    if view == "tree":
        import tree_sitter

        from src.parsers.tree_sitter_parser import get_language_from_extension
        from src.parsers.tsgm import TreeSitterGrammarManager
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

    # Graph database dependency and PageRank retrieval
    if view in ("summary", "deps"):
        try:
            graph_metrics = await asyncio.to_thread(api.get_file_dependencies, path.as_posix())
            if view == "summary":
                if "structure" in result and isinstance(result["structure"], dict):
                    result["structure"]["graph_metrics"] = graph_metrics
            else: # view == "deps"
                result["graph_metrics"] = graph_metrics
        except Exception as e:
            sys.stderr.write(f"[WARNING] Graph database query failed: {e}\n")
            sys.stderr.flush()

    if output_file:
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"[ERROR] Failed to write browse output to {output_file}: {e}\n")
            sys.stderr.flush()

        # Create a summarized version of result to return to the LLM
        summary_result = {"saved_to": output_file}
        if "query" in result:
            summary_result["query"] = result["query"]
        if "target" in result:
            summary_result["target"] = result["target"]
        if "type" in result:
            summary_result["type"] = result["type"]

        if "results" in result:
            summary_result["results_summary"] = [
                {k: v for k, v in r.items() if k != "body_text"}
                for r in result["results"]
            ]
        elif "structure" in result:
            struct = result["structure"]
            if isinstance(struct, list):
                summary_result["structure_summary_count"] = len(struct)
                summary_result["structure_preview"] = struct[:5]
            elif isinstance(struct, dict):
                if "tree" in struct:
                    summary_result["structure_preview"] = {"tree_length": len(struct["tree"])}
                else:
                    summary_result["structure"] = struct
            else:
                summary_result["structure"] = struct
        else:
            for k, v in result.items():
                if k not in summary_result:
                    if isinstance(v, list):
                        summary_result[f"{k}_summary_count"] = len(v)
                        summary_result[f"{k}_preview"] = v[:5]
                    else:
                        summary_result[k] = v

        return summary_result

    return result


async def do_metrics(target: str, what: List[str] = ["all"]) -> dict:
    path = safe_path(target)
    from src.mcp_server.index import find_repo_root
    repo_root = find_repo_root(str(path))
    what_set = {w.lower() for w in what}
    if "all" in what_set:
        what_set = {"oop", "complexity", "hotspots"}

    result: dict[str, Any] = {"target": path.as_posix(), "type": "file" if path.is_file() else "directory"}

    if path.is_file():
        content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')
        if "oop" in what_set or "complexity" in what_set:
            analyzer = OOPMetricsAnalyzer(use_ast=True, repo_path=repo_root)
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
            analyzer = OOPMetricsAnalyzer(use_ast=True, repo_path=repo_root)
            ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist"}
            files = []
            for r, d, fnames in os.walk(str(path)):
                d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
                for fname in fnames:
                    fp = Path(r) / fname
                    if fp.suffix in SUPPORTED:
                        files.append(fp)
            from src.utils.helpers import print_progress_bar
            import time
            total_files = len(files)
            start_time = time.time()
            for idx, fp in enumerate(files):
                print_progress_bar(
                    idx,
                    total_files,
                    prefix="[INFO] Calculating metrics",
                    suffix=f"({idx}/{total_files}) {fp.name[:30]:<30}",
                    stream=sys.stderr,
                    start_time=start_time
                )
                try:
                    txt = await asyncio.to_thread(fp.read_text, encoding='utf-8', errors='ignore')
                    analyzer.analyze_file(str(fp), txt, fp.suffix)
                except Exception:
                    pass
            print_progress_bar(
                total_files,
                total_files,
                prefix="[INFO] Calculating metrics",
                suffix=f"Completed ({total_files}/{total_files})",
                stream=sys.stderr,
                start_time=start_time
            )
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

    from src.core.config import conf
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
    from src.mcp_server.index import find_repo_root
    from src.mcp_server.indexer import CodebaseIndexer
    from mcp.server.lowlevel.server import request_ctx

    repo_root = find_repo_root(str(path))

    if path.is_dir():
        # Get request context to retrieve progress token and session
        try:
            ctx = request_ctx.get()
        except LookupError:
            ctx = None

        progress_token = None
        if ctx and ctx.meta:
            sys.stderr.write(f"[DEBUG] ctx.meta type: {type(ctx.meta)}, value: {ctx.meta}\n")
            sys.stderr.flush()
            if isinstance(ctx.meta, dict):
                progress_token = ctx.meta.get("progressToken") or ctx.meta.get("progress_token")
            else:
                progress_token = getattr(ctx.meta, "progressToken", getattr(ctx.meta, "progress_token", None))


        # Spawn index_worker.py in a subprocess using same python executable
        python_bin = sys.executable or "python"
        
        proc = await asyncio.create_subprocess_exec(
            python_bin, "-m", "src.mcp_server.index_worker", repo_root, str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace_root)
        )

        final_res = {}
        
        import time
        start_time = time.time()
        
        async def read_stdout():
            nonlocal final_res
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    if "result" in data:
                        final_res = data["result"]
                    elif "current" in data and "total" in data:
                        current = data["current"]
                        total = data["total"]
                        from src.utils.helpers import print_progress_bar
                        filename = os.path.basename(data.get("file", ""))
                        msg = f"Indexing {filename} ({current}/{total})"
                        print_progress_bar(
                            current,
                            total,
                            prefix="[INFO] Indexing",
                            suffix=f"({current}/{total}) {filename[:30]:<30}",
                            stream=sys.stderr,
                            start_time=start_time
                        )
                        
                        if ctx and progress_token is not None:
                            try:
                                req_id = str(ctx.request_id) if ctx.request_id is not None else None
                                await ctx.session.send_progress_notification(
                                    progress_token=progress_token,
                                    progress=current,
                                    total=total,
                                    message=msg,
                                    related_request_id=req_id
                                )
                            except Exception as e:
                                sys.stderr.write(f"[WARNING] Failed to send progress notification: {e}\n")
                                sys.stderr.flush()
                except Exception as e:
                    sys.stderr.write(f"[WARNING] Subprocess parse error: {e} for line: {line_str}\n")
                    sys.stderr.flush()

        async def read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str:
                    sys.stderr.write(f"[INDEX WORKER LOG] {line_str}\n")
                    sys.stderr.flush()

        await asyncio.gather(read_stdout(), read_stderr(), proc.wait())

        if proc.returncode != 0:
            raise RuntimeError(f"Index subprocess failed with exit code {proc.returncode}")

        final_res["target"] = path.as_posix()
        return final_res

    if path.suffix not in SUPPORTED:
        raise ValueError(f"Unsupported extension: {path.suffix}")

    content = await asyncio.to_thread(path.read_text, encoding='utf-8', errors='ignore')

    def _index_file():
        indexer = CodebaseIndexer(repo_path=repo_root)
        indexer.index_file(str(path), content, path.suffix, rebuild_fts=True)

    await asyncio.to_thread(_index_file)
    return {"status": "success", "target": path.as_posix(), "indexed_files": 1, "total_files_found": 1}


# Low-level Handlers

@server.list_tools()
async def handle_list_tools() -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="browse",
                description="Browse the codebase (structure, semantic search, or graph). If view='search', requires query. For structure, view can be: summary, classes, functions, deps, tree. For graph, view can be: callers (requires query as function name), callees (requires query as function name), impact.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "Absolute path to the target directory or file. Cannot be empty. If working on an external directory, provide its full absolute path."
                        },
                        "view": {"type": "string", "default": "summary"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                        "output_file": {
                            "type": "string",
                            "description": "Optional absolute path to a file where the full detailed JSON result should be saved. If provided, the tool response will be a compact summary to save tokens."
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 3,
                            "description": "Max depth for impact analysis (only applicable if view is 'impact')."
                        }
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
                        "target": {
                            "type": "string",
                            "description": "Absolute path to the target directory or file. Cannot be empty. If working on an external directory, provide its full absolute path."
                        },
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
                        "repo_path": {
                            "type": "string",
                            "description": "Absolute path to the target repository. Cannot be empty."
                        },
                        "output_path": {"type": "string"}
                    },
                    "required": ["repo_path", "output_path"]
                }
            ),
            types.Tool(
                name="update_index",
                description="Update the codebase Gorgonzola index via AST browsing.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "Absolute path to the target directory or file. Cannot be empty. If working on an external directory, provide its full absolute path."
                        }
                    },
                    "required": ["target"]
                }
            )
        ]
    )

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> types.CallToolResult:
    start_time = time.time()

    sys.stderr.write(f"[INFO] MCP Tool Call: '{name}' with arguments: {json.dumps(arguments)}\n")
    sys.stderr.flush()

    TOOL_CALLS.labels(tool=name).inc()

    try:
        def _normalize_target(t: Any) -> Any:
            if isinstance(t, dict) and "target" in t:
                t = t["target"]
            elif isinstance(t, str) and t.strip().startswith("{") and t.strip().endswith("}"):
                try:
                    import json
                    parsed = json.loads(t)
                    if isinstance(parsed, dict) and "target" in parsed:
                        t = parsed["target"]
                except Exception:
                    pass
            return t

        if name == "browse":
            target = _normalize_target(arguments.get("target"))
            if not isinstance(target, str):
                raise ValueError("target must be a string")
            res = await do_browse(
                target=target,
                view=arguments.get("view", "summary"),
                query=arguments.get("query"),
                limit=arguments.get("limit", 10),
                max_depth=arguments.get("max_depth", 3),
                output_file=arguments.get("output_file")
            )
        elif name == "metrics":
            target = _normalize_target(arguments.get("target"))
            if not isinstance(target, str):
                raise ValueError("target must be a string")
            res = await do_metrics(
                target=target,
                what=arguments.get("what", ["all"])
            )
        elif name == "report":
            repo_path = arguments.get("repo_path")
            output_path = arguments.get("output_path")
            if not isinstance(repo_path, str) or not isinstance(output_path, str):
                raise ValueError("repo_path and output_path must be strings")
            res = await do_report(
                repo_path=repo_path,
                output_path=output_path
            )
        elif name == "update_index":
            target = _normalize_target(arguments.get("target"))
            if not isinstance(target, str):
                raise ValueError("target must be a string")
            res = await do_update_index(
                target=target
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
            isError=True
        )

@server.list_prompts()
async def handle_list_prompts() -> types.ListPromptsResult:
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
                description="Update the codebase Gorgonzola index via AST browsing.",
                arguments=[
                    types.PromptArgument(name="target", description="Target path to index", required=True)
                ]
            )
        ]
    )

@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
    arguments = arguments or {}
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
            description="Update Gorgonzola index",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the update_index tool on target '{target}'."))]
        )
    raise ValueError(f"Prompt not found: {name}")

@server.completion()
async def handle_completion(
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None
) -> types.CompleteResult:
    result = None
    if isinstance(ref, types.PromptReference):
        if ref.name == "browse":
            if argument.name == "view":
                views = ["summary", "classes", "functions", "deps", "tree", "search"]
                result = types.Completion(
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
                    elif os.path.isdir(m):
                        files.append(m)
                    if len(files) >= 50:
                        break
                result = types.Completion(values=sorted(files)[:20], hasMore=len(files) > 20)

        elif ref.name == "metrics":
            if argument.name == "what":
                whats = ["oop", "complexity", "hotspots", "all"]
                result = types.Completion(
                    values=[w for w in whats if w.startswith(argument.value.lower())],
                    hasMore=False
                )
            elif argument.name == "target":
                val = argument.value or ""
                matches = glob.glob(val + "*")
                paths = sorted(matches)[:20]
                result = types.Completion(values=paths, hasMore=len(matches) > 20)

        elif ref.name == "report":
            if argument.name == "repo_path":
                val = argument.value or ""
                matches = [m for m in glob.glob(val + "*") if os.path.isdir(m)]
                paths = sorted(matches)[:20]
                result = types.Completion(values=paths, hasMore=len(matches) > 20)

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
                    elif os.path.isdir(m):
                        files.append(m)
                    if len(files) >= 50:
                        break
                result = types.Completion(values=sorted(files)[:20], hasMore=len(files) > 20)

    return types.CompleteResult(
        completion=result if result is not None else types.Completion(values=[], total=None, hasMore=None)
    )


