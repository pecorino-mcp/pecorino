import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.core.errors import AnalysisError, IndexNotFoundError, SecurityValidationError
from src.mcp_server.middleware.caching import _get_cached_api, clear_index_cache
from src.mcp_server.middleware.security import check_suspicious, safe_path
from src.mcp_server.middleware.sync import _auto_sync_stale
from src.mcp_server.prometheus_metrics import FTS_SCAN_DURATION

logger = logging.getLogger(__name__)

_fts_rebuild_lock = threading.Lock()
INDEX_TIMEOUT_S = 300
MAX_QUERY_LEN = 200
MAX_LIMIT = 100
MAX_CODE_LINES = 300
MAX_DEPTH = 10
from mcp.server import ServerRequestContext

# ── Search modes ──────────────────────────────────────────────
ALLOWED_MODES = frozenset({
    "fts",              # Full-text keyword search (default)
    "callers",          # Who calls symbol X?
    "callees",          # What does symbol X call?
    "impact",           # Deep dependency trace
    "usages",           # Combined: search + callers (replaces explain_symbol)
    "intent",           # Preset AST queries (all_classes, dead_code, etc.)
    "dsl",              # Custom JSON DSL query
    "functional-analysis",  # Functional purity analysis
    "cypher",           # Native Cypher read-only queries
    "hybrid",           # Hybrid Vector+BM25 search
    "community",        # Semantic neighborhood of a symbol
    "trace",             # Multi-hop call graph traversal (like CBM trace_path)
})

# ── Intent-based presets (from query_codebase) ────────────────
INTENT_PRESETS: dict[str, dict] = {
    "all_classes": {
        "select": "nodes",
        "where": {"kind": {"in": ["class", "interface"]}},
        "limit": 50,
    },
    "all_functions": {
        "select": "nodes",
        "where": {"kind": {"in": ["function", "method"]}},
        "limit": 50,
    },
    "files_by_language": {
        "select": "files",
        "limit": 100,
    },
    "entry_points": {
        "_graph_intent": "entry_points",
        "select": "nodes",
        "where": {"kind": {"in": ["function", "method"]}},
        "limit": 20,
    },
    "dead_code": {
        "_graph_intent": "dead_code",
        "select": "nodes",
        "where": {"kind": {"in": ["function", "method"]}},
        "limit": 50,
    },
}


def _cap_body(body: str) -> str:
    """Truncate body_text to MAX_CODE_LINES lines."""
    if not body:
        return ''
    lines = body.split('\n')
    if len(lines) > MAX_CODE_LINES:
        return '\n'.join(lines[:MAX_CODE_LINES]) + f"\n... (truncated at {MAX_CODE_LINES} lines)"
    return body


# ═══════════════════════════════════════════════════════════════
#  Unified search entry point
# ═══════════════════════════════════════════════════════════════

async def do_search(
    target: str,
    query: Optional[str] = None,
    mode: str = "hybrid",
    limit: int = 10,
    offset: int = 0,
    include_source: bool = False,
    auto_expand_source: bool = True,
    max_depth: int = 3,
    intent: Optional[str] = None,
    query_json: Optional[str | Dict[str, Any]] = None,
    allow_external: bool = False,
    output_file: Optional[str] = None,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Unified search and analysis tool.

    Modes:
      fts       — Full-text keyword search. query required for dirs.
      callers   — Who calls symbol X? (query = symbol name, required)
      callees   — What does symbol X call? (query = symbol name, required)
      impact    — Deep dependency trace from a file/directory.
      usages    — Combined: FTS search + callers in one call.
      intent    — Preset AST queries (use 'intent' param).
      dsl       — Custom JSON DSL query (use 'query_json' param).
      functional-analysis — Functional purity analysis.
      cypher    — Native Cypher read-only queries (query = cypher string, required).
      trace     — Multi-hop call graph traversal (query = symbol name, required).
    """
    mode = mode.strip().lower()
    if mode not in ALLOWED_MODES:
        raise SecurityValidationError(
            f"Invalid search mode: '{mode}'",
            valid_values=sorted(ALLOWED_MODES),
            suggestion="Use one of the listed mode values.",
        )

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    max_depth = max(1, min(int(max_depth), MAX_DEPTH))

    if query:
        query = query.strip()[:MAX_QUERY_LEN]
        if any(c in query for c in "\x00\n\r"):
            raise SecurityValidationError("Invalid characters in query")
        check_suspicious(query, "query")

    # ── Route to mode-specific handlers ───────────────────────
    if mode in ("fts", "hybrid"):
        return await _do_fts(target, query, mode, limit, offset, include_source,
                             auto_expand_source, output_file, allow_external, ctx)
    elif mode in ("callers", "callees"):
        return await _do_callers_callees(target, mode, query, limit, offset,
                                         allow_external, ctx)
    elif mode == "impact":
        return await _do_impact(target, limit, offset, max_depth,
                                allow_external, ctx)
    elif mode == "usages":
        return await _do_usages(target, query, limit, allow_external, ctx)
    elif mode == "intent":
        return await _do_intent(target, intent, allow_external, ctx)
    elif mode == "dsl":
        return await _do_dsl(target, query_json, allow_external, ctx)
    elif mode == "functional-analysis":
        return await _do_functional_analysis(target, allow_external, ctx)
    elif mode == "cypher":
        return await _do_cypher(target, query, allow_external, ctx)
    elif mode == "community":
        return await _do_community(target, query, limit, offset, allow_external, ctx)
    elif mode == "trace":
        return await _do_trace(target, query, max_depth, allow_external, ctx)

    return {"error": "Unknown mode"}


# ═══════════════════════════════════════════════════════════════
#  Mode: fts (Full-Text Search) — original search logic
# ═══════════════════════════════════════════════════════════════

async def _do_fts(
    target: str,
    query: Optional[str],
    mode: str,
    limit: int,
    offset: int,
    include_source: bool,
    auto_expand_source: bool,
    output_file: Optional[str],
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    index = _get_cached_api(repo_root, db_path, "index")

    # --- File target: return nodes directly ---
    if path.is_file():
        nodes = await asyncio.to_thread(index.get_file_nodes, str(path))
        if query:
            q_lower = query.lower()
            filtered_nodes = []
            for n in nodes:
                if q_lower in n['name'].lower():
                    filtered_nodes.append(n)
                elif q_lower in n.get('body_text', '').lower():
                    filtered_nodes.append(n)
            nodes = filtered_nodes
        nodes = nodes[offset:offset+limit]

        # Auto-expand: include source when few results
        auto_expanded = False
        effective_include = include_source
        if not include_source and auto_expand_source and len(nodes) <= 3:
            effective_include = True
            auto_expanded = True

        if effective_include:
            for n in nodes:
                n['body_text'] = _cap_body(n.get('body_text', ''))
        else:
            for n in nodes:
                body = n.pop('body_text', "")
                if body and query:
                    lines = body.split('\n')
                    q_lower = query.lower()
                    for i, line in enumerate(lines):
                        if q_lower in line.lower():
                            start_idx = max(0, i - 1)
                            end_idx = min(len(lines), i + 2)
                            n["matched_snippet"] = "\n".join(lines[start_idx:end_idx]).strip()
                            break
        result = {"query": query, "results": nodes, "search_status": "ok"}
        if auto_expanded:
            result["auto_expanded"] = True
        return result

    # --- Directory target: FTS search ---
    if not query:
        raise SecurityValidationError(
            "Query is required when searching a directory",
            suggestion="Provide a search query string, or target a specific file path instead.",
        )

    # Lazy FTS rebuild if stale
    if not index.has_fts_index() or index.is_fts_dirty():
        from src.mcp_server.index_db import CodeSearchIndex

        def _rebuild_fts():
            write_index = CodeSearchIndex(db_path=db_path, read_only=False)
            try:
                write_index.ensure_fts()
            finally:
                write_index.close()

        with _fts_rebuild_lock:
            if not index.has_fts_index() or index.is_fts_dirty():
                logger.info("Lazy FTS rebuild triggered for %s", db_path)
                clear_index_cache()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_rebuild_fts),
                        timeout=INDEX_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    raise AnalysisError(
                        f"FTS rebuild timed out after {INDEX_TIMEOUT_S}s. "
                        f"Run 'update_index' manually to rebuild."
                    )
                clear_index_cache()
                index = _get_cached_api(repo_root, db_path, "index")

    _fts_start = time.time()
    results = await asyncio.to_thread(index.search, query, limit, path.as_posix(), offset, mode=mode)
    FTS_SCAN_DURATION.observe(time.time() - _fts_start)

    # Auto-expand: include source when few results
    auto_expanded = False
    if not include_source and auto_expand_source and len(results) <= 3 and not output_file:
        include_source = True
        auto_expanded = True

    if include_source:
        for r in results:
            r['body_text'] = _cap_body(r.get('body_text', ''))
    elif not output_file:
        for r in results:
            body = r.pop("body_text", "")
            if body and query:
                # Find the first line containing the query (case-insensitive)
                lines = body.split('\n')
                q_lower = query.lower()
                for i, line in enumerate(lines):
                    if q_lower in line.lower():
                        # Grab +/- 1 line around the match
                        start_idx = max(0, i - 1)
                        end_idx = min(len(lines), i + 2)
                        snippet_lines = lines[start_idx:end_idx]
                        r["matched_snippet"] = "\n".join(snippet_lines).strip()
                        break

    result = {"query": query, "results": results, "search_status": "ok"}
    if auto_expanded:
        result["auto_expanded"] = True
    return result


# ═══════════════════════════════════════════════════════════════
#  Mode: callers / callees — from graph.py
# ═══════════════════════════════════════════════════════════════

async def _do_callers_callees(
    target: str,
    mode: str,
    symbol: Optional[str],
    limit: int,
    offset: int,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    if not symbol:
        raise SecurityValidationError(f"'query' (symbol name) is required for {mode} mode")

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    api = _get_cached_api(repo_root, db_path, "graph")

    if mode == "callers":
        callers = await asyncio.to_thread(api.find_callers, symbol)
        page = callers[offset:offset+limit]
        result = {"target": symbol, "mode": mode, "callers": page}
        # Auto-expand: include source signatures when few results
        if len(page) <= 5 and page:
            index = _get_cached_api(repo_root, db_path, "index")
            for c in page:
                fp = c.get("filepath") or c.get("file")
                if fp:
                    try:
                        file_nodes = await asyncio.to_thread(index.get_file_nodes, fp)
                        cname = c.get("name", "")
                        for n in file_nodes:
                            if n.get("name") == cname:
                                body = n.get("body_text", "")
                                sig_lines = body.split("\n")[:5] if body else []
                                c["signature_preview"] = "\n".join(sig_lines)
                                break
                    except Exception:
                        pass
            result["auto_expanded"] = True
        if len(callers) > limit:
            result["total_count"] = len(callers)
        return result

    else:  # callees
        callees = await asyncio.to_thread(api.find_callees, symbol)
        page = callees[offset:offset+limit]
        result = {"target": symbol, "mode": mode, "callees": page}
        if len(callees) > limit:
            result["total_count"] = len(callees)
        return result


# ═══════════════════════════════════════════════════════════════
#  Mode: impact — from graph.py
# ═══════════════════════════════════════════════════════════════

async def _do_impact(
    target: str,
    limit: int,
    offset: int,
    max_depth: int,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    api = _get_cached_api(repo_root, db_path, "graph")

    deps = await asyncio.to_thread(api.impact_analysis, path.as_posix(), max_depth)
    page = deps[offset:offset+limit]
    result = {"target": path.as_posix(), "mode": "impact", "dependent_files": page}
    if len(deps) > limit:
        pkg_counts: dict[str, int] = {}
        for d in deps:
            fp = d if isinstance(d, str) else (d.get("filepath", "") if isinstance(d, dict) else str(d))
            parts = fp.split("/")
            pkg = "/".join(parts[:3]) if len(parts) >= 3 else fp
            pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
        top_packages = sorted(pkg_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        result["summary"] = {
            "total_impacted_files": len(deps),
            "top_impacted_packages": [
                {"package": pkg, "file_count": count}
                for pkg, count in top_packages
            ],
            "max_depth_reached": max_depth,
            "hint": "Use offset/limit to paginate, or narrow target to a specific subdirectory.",
        }
    return result


# ═══════════════════════════════════════════════════════════════
#  Mode: usages — replaces explain_symbol
# ═══════════════════════════════════════════════════════════════

async def _do_usages(
    target: str,
    query: Optional[str],
    limit: int,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    """Combined: FTS search + callers in one call."""
    if not query:
        raise SecurityValidationError("'query' (symbol name) is required for usages mode")

    # 1. Search for the symbol definition
    try:
        search_res = await _do_fts(
            target=target, query=query, mode="fts", limit=5, offset=0,
            include_source=True, auto_expand_source=True,
            output_file=None, allow_external=allow_external, ctx=ctx
        )
    except Exception as e:
        search_res = {"error": f"Search failed: {e}"}

    # 2. Find callers
    try:
        callers_res = await _do_callers_callees(
            target=target, mode="callers", symbol=query,
            limit=5, offset=0, allow_external=allow_external, ctx=ctx
        )
    except Exception as e:
        callers_res = {"error": f"Callers analysis failed: {e}"}

    return {
        "symbol": query,
        "target": target,
        "search_results": search_res.get("results", search_res),
        "callers": callers_res.get("callers", callers_res),
    }


# ═══════════════════════════════════════════════════════════════
#  Mode: intent — from query_codebase (intent presets)
# ═══════════════════════════════════════════════════════════════

async def _do_intent(
    target: str,
    intent: Optional[str],
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    if not intent:
        raise SecurityValidationError(
            "'intent' parameter is required for intent mode",
            valid_values=list(INTENT_PRESETS.keys()),
            suggestion="Use one of the listed intent values.",
        )
    if intent not in INTENT_PRESETS:
        raise SecurityValidationError(
            f"Unknown intent: '{intent}'",
            valid_values=list(INTENT_PRESETS.keys()),
            suggestion="Use one of the listed intent values.",
        )

    query_dict = dict(INTENT_PRESETS[intent])  # copy to avoid mutation
    return await _do_dsl(target, query_dict, allow_external, ctx)


# ═══════════════════════════════════════════════════════════════
#  Mode: dsl — from query_codebase (custom JSON DSL)
# ═══════════════════════════════════════════════════════════════

async def _do_dsl(
    target: str,
    query_json: Optional[str | Dict[str, Any]],
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    import json as json_mod
    import os

    if query_json is None:
        raise SecurityValidationError(
            "'query_json' parameter is required for dsl mode",
            suggestion="Provide a JSON object like {\"select\": \"nodes\", \"where\": {\"kind\": \"function\"}}",
        )

    if isinstance(query_json, str):
        try:
            query_json = json_mod.loads(query_json)
        except json_mod.JSONDecodeError:
            raise SecurityValidationError(
                "Invalid JSON in query_json parameter",
                suggestion="Provide valid JSON, or use mode='intent' for common queries.",
            )

    if not isinstance(query_json, dict):
        raise SecurityValidationError(
            "query_json must be a JSON object",
            suggestion='Provide a JSON object like {"select": "nodes", "where": {"kind": "function"}}',
        )

    # Extract and strip internal graph intent marker
    graph_intent = query_json.pop("_graph_intent", None)

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    from src.mcp_server.middleware.sync import _auto_sync_stale
    await _auto_sync_stale(repo_root, db_path, str(path))

    # ── Graph-dependent intents ───────────────────────────────
    if graph_intent in ("entry_points", "dead_code"):
        try:
            graph_api = _get_cached_api(repo_root, db_path, "graph")
        except Exception:
            return {
                "status": "error",
                "error_type": "index_missing",
                "message": "Graph index not available for this repository.",
                "suggestion": "Run 'update_index' first to build the graph, then retry this intent.",
            }

        index = _get_cached_api(repo_root, db_path, "index")
        limit = min(query_json.get("limit", 50), 100)
        nodes = await asyncio.to_thread(index.get_dir_nodes, str(path))
        fn_nodes = [n for n in nodes if n.get("kind") in ("function", "method")]

        caller_counts = {}
        try:
            cypher = '''
            MATCH (caller)-[:CALLS]->(callee)
            RETURN callee.name AS name, COUNT(caller) AS caller_count
            '''
            rows = await asyncio.to_thread(graph_api.graph.query, cypher)
            for r in rows:
                if r.get('name'):
                    caller_counts[r['name']] = r['caller_count']
        except Exception as e:
            logger.warning(f"Bulk caller count query failed: {e}")

        results = []
        for node in fn_nodes:
            name = node.get("name", "")
            caller_count = caller_counts.get(name, 0)

            if graph_intent == "entry_points" and caller_count >= 5:
                results.append({**node, "caller_count": caller_count})
            elif graph_intent == "dead_code" and caller_count == 0:
                results.append({**node, "caller_count": 0})

        if graph_intent == "entry_points":
            results.sort(key=lambda x: x.get("caller_count", 0), reverse=True)

        for r in results:
            r.pop("body_text", None)

        return {
            "status": "ok",
            "intent": graph_intent,
            "results": results[:limit],
            "total_candidates": len(fn_nodes),
        }

    # ── Standard DSL path ─────────────────────────────────────
    from src.mcp_server.dsl.compiler import DSLCompiler
    from src.mcp_server.index_db import get_graph_path_for_repo
    from src.mcp_server.prometheus_metrics import GRAPH_DB_SIZE

    sql_query, cypher_query, sql_params = DSLCompiler.compile(query_json, db_path="main")

    index = _get_cached_api(repo_root, db_path, "index")
    conn = index._conn

    results = []

    if cypher_query:
        graph_api = _get_cached_api(repo_root, db_path, "graph")
        graph_res = await asyncio.to_thread(graph_api.graph.query, cypher_query)

        def get_dir_size(dir_path: str) -> int:
            total = 0
            try:
                for dirpath, _, filenames in os.walk(dir_path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if not os.path.islink(fp):
                            total += os.path.getsize(fp)
            except Exception as e:
                logger.warning(f"Failed to get graph db size: {e}")
            return total

        graph_db_dir = get_graph_path_for_repo(db_path)
        if os.path.exists(graph_db_dir):
            GRAPH_DB_SIZE.set(get_dir_size(graph_db_dir))

        matching_ids = []
        for row in graph_res:
            matching_ids.append(row['id'])

        if not matching_ids:
            return {"status": "ok", "results": [], "note": "No graph matches found"}

        placeholders = ",".join(["?" for _ in matching_ids])
        limit_idx = sql_query.upper().rfind("LIMIT")
        if limit_idx != -1:
            sql_query = f"{sql_query[:limit_idx]} AND id IN ({placeholders}) {sql_query[limit_idx:]}"
        else:
            sql_query = f"{sql_query} AND id IN ({placeholders})"

        sql_params.extend(matching_ids)

    try:
        df = await asyncio.to_thread(conn.execute, sql_query, sql_params)
        columns = [desc[0] for desc in df.description]
        rows = df.fetchall()
        for r in rows:
            results.append(dict(zip(columns, r)))
    except Exception as e:
        logger.warning(f"DSL SQL query failed: {e}")
        return {"status": "error", "error": str(e), "sql": sql_query}

    return {
        "status": "ok",
        "query": query_json,
        "results": results
    }


# ═══════════════════════════════════════════════════════════════
#  Mode: functional-analysis — from graph.py
# ═══════════════════════════════════════════════════════════════

async def _do_functional_analysis(
    target: str,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    import json as json_mod

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    api = _get_cached_api(repo_root, db_path, "graph")

    fa_result = await asyncio.to_thread(api.analyze_functional_purity)
    ret = {"target": path.as_posix(), "mode": "functional-analysis", "functional_analysis": fa_result}

    try:
        dumped = json_mod.dumps(ret)
        if len(dumped) > 50000:
            output_file = f".mcp_outputs/search_functional-analysis_{int(time.time())}.json"
            from src.mcp_server.middleware.security import safe_output_path
            out_path = safe_output_path(output_file)
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(dumped)
            return {
                "target": path.as_posix(),
                "mode": "functional-analysis",
                "saved_to": str(out_path),
                "summary": f"Functional analysis output is too large ({len(dumped)} chars). Results saved to file.",
            }
    except Exception:
        pass
    return ret


# ═══════════════════════════════════════════════════════════════
#  Mode: cypher — Native Cypher Query Mode
# ═══════════════════════════════════════════════════════════════

async def _do_cypher(
    target: str,
    query: Optional[str],
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    if not query:
        raise SecurityValidationError("'query' is required for cypher mode")



    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(f"External repository at '{repo_root}' has not been indexed yet.")

    from src.mcp_server.middleware.sync import _auto_sync_stale
    await _auto_sync_stale(repo_root, db_path, str(path))
    api = _get_cached_api(repo_root, db_path, "graph")

    try:
        rows = await asyncio.to_thread(api.graph.query, query)
        return {
            "status": "ok",
            "mode": "cypher",
            "results": rows
        }
    except Exception as e:
        logger.warning(f"Cypher query failed: {e}")
        return {"status": "error", "error": str(e), "cypher": query}

# ═══════════════════════════════════════════════════════════════
#  Mode: community (Semantic Neighborhood)
# ═══════════════════════════════════════════════════════════════

async def _do_community(
    target: str,
    query: Optional[str],
    limit: int,
    offset: int,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    if not query:
        raise SecurityValidationError("Query is required for community mode")
        
    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    index = _get_cached_api(repo_root, db_path, "index")
    
    conn = index._conn
    def _fetch():
        res = conn.execute(
            "SELECT community_id, name FROM code_nodes WHERE name LIKE ? OR name = ? ORDER BY pagerank DESC LIMIT 1", 
            (f"%{query}%", query)
        )
        return res.fetchone()
        
    row = await asyncio.to_thread(_fetch)
    
    if not row or row[0] is None:
        return {"status": "ok", "results": [], "message": f"No community found for '{query}'"}
        
    community_id = row[0]
    matched_name = row[1]
    
    nodes = await asyncio.to_thread(index.get_community_nodes, community_id)
    
    sliced_nodes = nodes[offset:offset+limit]
    
    return {
        "status": "ok", 
        "community_id": community_id,
        "matched_symbol": matched_name,
        "total_in_community": len(nodes),
        "results": sliced_nodes
    }


# ═══════════════════════════════════════════════════════════════
#  Mode: trace (Multi-hop call graph traversal)
# ═══════════════════════════════════════════════════════════════

async def _do_trace(
    target: str,
    query: Optional[str],
    max_depth: int,
    allow_external: bool,
    ctx: Optional[ServerRequestContext]
) -> dict:
    """Trace call graph paths from a symbol, similar to CBM's trace_path.

    Returns callers and callees at each hop level up to max_depth.
    """
    if not query:
        raise SecurityValidationError("'query' (symbol name) is required for trace mode")

    path = safe_path(target, allow_external)
    from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo
    repo_root = find_repo_root(str(path))
    db_path = get_db_path_for_repo(repo_root)

    import os
    if allow_external and not os.path.exists(db_path):
        raise IndexNotFoundError(
            f"External repository at '{repo_root}' has not been indexed yet. "
            f"Please run the 'update_index' tool with allow_external=True on this target first."
        )

    await _auto_sync_stale(repo_root, db_path, str(path))
    api = _get_cached_api(repo_root, db_path, "graph")

    result = await asyncio.to_thread(api.trace_calls, query, "both", max_depth)
    
    callers = result.get("callers", [])
    callees = result.get("callees", [])
    
    summary_parts = []
    if callees:
        fuzzy = sum(1 for c in callees if c.get("edge_type") == "LIKELY_CALLS")
        data = sum(1 for c in callees if c.get("edge_type") == "DATA_FLOWS_TO")
        summary_parts.append(f"{len(callees)} callees ({fuzzy} fuzzy, {data} data-flow)")
    if callers:
        fuzzy = sum(1 for c in callers if c.get("edge_type") == "LIKELY_CALLS")
        data = sum(1 for c in callers if c.get("edge_type") == "DATA_FLOWS_TO")
        summary_parts.append(f"{len(callers)} callers ({fuzzy} fuzzy, {data} data-flow)")
        
    if summary_parts:
        result["summary"] = " | ".join(summary_parts)
    else:
        result["summary"] = "No relations found."

    return {"status": "ok", "mode": "trace", **result}
