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
            description="Browse codebase structure (tree, deps, classes, functions, pagerank, summary).",
            arguments=[
                types.PromptArgument(name="target", description="Target path to browse", required=False),
                types.PromptArgument(name="view", description="View type (tree, classes, deps, all, pagerank, summary)", required=False)
            ]
        ),
        types.Prompt(
            name="search",
            description="Unified search and analysis. Supports FTS, callers/callees, impact, usages, intent presets, and DSL queries.",
            arguments=[
                types.PromptArgument(name="query", description="Search query, keyword, or symbol name", required=False),
                types.PromptArgument(name="target", description="Target path (file or directory)", required=False),
                types.PromptArgument(name="mode", description="Search mode (fts, callers, callees, impact, usages, intent, dsl, functional-analysis)", required=False),
                types.PromptArgument(name="intent", description="Preset query intent (all_classes, all_functions, entry_points, dead_code, files_by_language)", required=False),
                types.PromptArgument(name="include_source", description="Include source code in results", required=False)
            ]
        ),
        types.Prompt(
            name="update_index",
            description="Update the AST index for the codebase and return a structural summary.",
            arguments=[
                types.PromptArgument(name="target", description="Target path to update index for", required=False)
            ]
        ),

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
        target_str = f" on target '{target}'" if target else ""
        return types.GetPromptResult(
            description="Browse codebase structure.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the browse tool{target_str} with view '{view}'."))]
        )
    elif name == "search":
        target = arguments.get("target", "")
        query = arguments.get("query", "")
        mode = arguments.get("mode", "fts")
        intent = arguments.get("intent", "")
        target_str = f" on target '{target}'" if target else ""
        if mode == "intent" and intent:
            return types.GetPromptResult(
                description="Search the codebase.",
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the search tool{target_str} with mode='intent' and intent='{intent}'."))]
            )
        elif mode in ("callers", "callees", "usages"):
            return types.GetPromptResult(
                description="Search the codebase.",
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the search tool{target_str} with mode='{mode}' for symbol '{query}'."))]
            )
        else:
            return types.GetPromptResult(
                description="Search the codebase.",
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the search tool{target_str} with mode='{mode}' for query '{query}'."))]
            )
    elif name == "update_index":
        target = arguments.get("target", "")
        target_str = f" on target '{target}'" if target else ""
        return types.GetPromptResult(
            description="Update codebase index.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please use the update_index tool{target_str}."))]
        )

    elif name == "metrics":
        target = arguments.get("target", "")
        what = arguments.get("what", "all")
        target_str = f" on target '{target}'" if target else ""
        return types.GetPromptResult(
            description="Calculate metrics.",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Please calculate {what} metrics{target_str}."))]
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

        if ref.name in ("browse", "search", "metrics", "update_index") and argument.name == "target":
            val = (context or {}).get("target", "")
            return _complete_target_path(val)



        elif ref.name == "browse" and argument.name == "view":
            views = ["classes", "functions", "deps", "tree", "all", "pagerank", "summary"]
            val = (context or {}).get("view", "")
            return types.Completion(
                values=[v for v in views if v.startswith(val.lower())],
                has_more=False
            )

        elif ref.name == "search" and argument.name == "mode":
            modes = ["fts", "callers", "callees", "impact", "usages", "intent", "dsl", "functional-analysis"]
            val = (context or {}).get("mode", "")
            return types.Completion(
                values=[m for m in modes if m.startswith(val.lower())],
                has_more=False
            )

        elif ref.name == "search" and argument.name == "intent":
            intents = ["all_classes", "all_functions", "files_by_language", "entry_points", "dead_code"]
            val = (context or {}).get("intent", "")
            return types.Completion(
                values=[i for i in intents if i.startswith(val.lower())],
                has_more=False
            )

    return types.CompleteResult(
        completion=result if result is not None else types.Completion(values=[], total=None, has_more=None)
    )
