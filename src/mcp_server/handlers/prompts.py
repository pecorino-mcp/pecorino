import glob
import logging
import os

import mcp_types as types
from mcp.server import ServerRequestContext

from src.core.constants import SUPPORTED_EXTENSIONS as SUPPORTED

from src.mcp_server.context_helper import PecorinoContext

logger = logging.getLogger(__name__)

async def handle_list_prompts(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None = None
) -> types.ListPromptsResult:
    helper = PecorinoContext(ctx)
    role = helper.role

    prompts = [
        types.Prompt(
            name="browse",
            description="Browse codebase structure (tree, deps, classes, functions).",
            arguments=[
                types.PromptArgument(name="target", description="Target path to browse", required=False),
                types.PromptArgument(name="view", description="View type (tree, classes, deps, all)", required=False)
            ]
        ),
        types.Prompt(
            name="search",
            description="Search the codebase for symbols or keywords. Use include_source=True to retrieve source code.",
            arguments=[
                types.PromptArgument(name="query", description="The search query or keyword", required=False),
                types.PromptArgument(name="target", description="Target path (file or directory)", required=False),
                types.PromptArgument(name="include_source", description="Include source code in results", required=False)
            ]
        ),
        types.Prompt(
            name="analyze",
            description="Run graph analysis such as callers, callees, impact analysis, or pagerank.",
            arguments=[
                types.PromptArgument(name="analysis", description="Analysis type (callers, callees, impact, pagerank)", required=True),
                types.PromptArgument(name="target", description="Target path", required=False),
                types.PromptArgument(name="symbol", description="Symbol name for callers/callees", required=False)
            ]
        ),
        types.Prompt(
            name="query_codebase",
            description="Execute a JSON-based DSL query against the codebase AST and graph.",
            arguments=[
                types.PromptArgument(name="query_json", description="JSON string of the query DSL", required=True),
                types.PromptArgument(name="target", description="Target path", required=False)
            ]
        ),
        types.Prompt(
            name="update_index",
            description="Update the AST index for the codebase and return a structural summary.",
            arguments=[
                types.PromptArgument(name="target", description="Target path to update index for", required=False)
            ]
        )
    ]
    
    if role == "admin":
        prompts.append(
            types.Prompt(
                name="metrics",
                description="Calculate OOP metrics, cyclomatic complexity, or hotspot risk analysis. (Admin only)",
                arguments=[
                    types.PromptArgument(name="target", description="Target path", required=True),
                    types.PromptArgument(name="what", description="Which analyses to run (oop, complexity, hotspots, all)", required=False)
                ]
            )
        )
        
    return types.ListPromptsResult(prompts=prompts)

async def handle_get_prompt(
    ctx: ServerRequestContext,
    params: types.GetPromptRequestParams
) -> types.GetPromptResult:
    name = params.name
    arguments = params.arguments or {}
    
    if name == "browse":
        target = arguments.get("target", "")
        view = arguments.get("view", "tree")
        return types.GetPromptResult(
            description="Browse codebase structure.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the browse tool on target '{target}' with view '{view}'."))]
        )
    elif name == "search":
        target = arguments.get("target", "")
        query = arguments.get("query", "")
        return types.GetPromptResult(
            description="Search the codebase.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the search tool on target '{target}' for query '{query}'."))]
        )
    elif name == "analyze":
        target = arguments.get("target", "")
        analysis = arguments.get("analysis", "")
        symbol = arguments.get("symbol", "")
        msg = f"Please run {analysis} analysis"
        if target:
            msg += f" on {target}"
        if symbol:
            msg += f" for symbol {symbol}"
        return types.GetPromptResult(
            description="Analyze the codebase.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=msg))]
        )
    elif name == "query_codebase":
        target = arguments.get("target", "")
        query_json = arguments.get("query_json", "")
        return types.GetPromptResult(
            description="Query the codebase using JSON DSL.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please execute the following JSON query on target '{target}':\n{query_json}"))]
        )
    elif name == "update_index":
        target = arguments.get("target", "")
        return types.GetPromptResult(
            description="Update codebase index.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the update_index tool on target '{target}'."))]
        )
    elif name == "metrics":
        target = arguments.get("target", "")
        what = arguments.get("what", "all")
        return types.GetPromptResult(
            description="Calculate metrics.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please calculate {what} metrics on target '{target}'."))]
        )
    raise ValueError(f"Prompt not found: {name}")

async def handle_completion(
    ctx: ServerRequestContext,
    params: types.CompleteRequestParams
) -> types.CompleteResult:
    ref = params.ref
    argument = params.argument
    context = params.context
    result = None
    if isinstance(ref, types.PromptReference):
        def _complete_target_path(val: str, filter_supported: bool = True) -> types.Completion:
            if ".." in val or "\\x00" in val:
                return types.Completion(values=[], has_more=False)
            matches = glob.glob(val + "*") + glob.glob(val + "**/*", recursive=True)
            files = []
            for m in matches:
                if os.path.isfile(m):
                    if filter_supported:
                        ext = os.path.splitext(m)[1].lower()
                        if ext not in SUPPORTED:
                            continue
                    files.append(m)
                elif os.path.isdir(m):
                    files.append(m)
                if len(files) >= 50:
                    break
            return types.Completion(values=sorted(files)[:20], has_more=len(files) > 20)

        if ref.name in ("browse", "search", "analyze", "metrics", "update_index", "query_codebase") and argument.name == "target":
            val = (context or {}).get("target", "")
            return _complete_target_path(val)
            
        elif ref.name == "browse" and argument.name == "view":
            views = ["classes", "functions", "deps", "tree", "all"]
            val = (context or {}).get("view", "")
            return types.Completion(
                values=[v for v in views if v.startswith(val.lower())],
                has_more=False
            )
            
        elif ref.name == "analyze" and argument.name == "analysis":
            analyses = ["callers", "callees", "impact", "pagerank", "functional-analysis"]
            val = (context or {}).get("analysis", "")
            return types.Completion(
                values=[a for a in analyses if a.startswith(val.lower())],
                has_more=False
            )
            
    return types.CompleteResult(
        completion=result if result is not None else types.Completion(values=[], total=None, has_more=None)
    )
