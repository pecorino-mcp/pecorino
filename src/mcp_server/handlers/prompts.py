import glob
import logging
import os

import mcp.types as types
from mcp.server import ServerRequestContext

from src.core.constants import SUPPORTED_EXTENSIONS as SUPPORTED

logger = logging.getLogger(__name__)

async def handle_list_prompts(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None = None
) -> types.ListPromptsResult:
    return types.ListPromptsResult(
        prompts=[
            types.Prompt(
                name="browse",
                description="Browse codebase structure (tree, deps, summary, classes, functions).",
                arguments=[
                    types.PromptArgument(name="target", description="Target path to browse", required=False),
                    types.PromptArgument(name="view", description="View type (summary, classes, deps, tree)", required=False)
                ]
            ),
            types.Prompt(
                name="search",
                description="Perform a semantic Full-Text Search (FTS) across the codebase for a symbol or keyword.",
                arguments=[
                    types.PromptArgument(name="query", description="The search query or keyword", required=True),
                    types.PromptArgument(name="target", description="Target path", required=False)
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
            )
        ]
    )

async def handle_get_prompt(
    ctx: ServerRequestContext,
    params: types.GetPromptRequestParams
) -> types.GetPromptResult:
    name = params.name
    arguments = params.arguments or {}
    
    if name == "browse":
        target = arguments.get("target", "")
        view = arguments.get("view", "summary")
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
            views = ["summary", "classes", "functions", "deps", "tree"]
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
