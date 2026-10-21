import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def find_lsp_resolutions(lsp_client, tree, file_str: str) -> list:
    """Resolve call positions in a syntax tree using the given LSP client."""
    if not lsp_client:
        return []

    positions = []

    def visit(n):
        if n.type in ('call_expression', 'method_invocation', 'call'):
            func_node = n.child_by_field_name('function') or n.child_by_field_name('name')
            if not func_node and n.children:
                func_node = n.children[0]
            if func_node:
                line = func_node.start_point[0] + 1
                char = func_node.start_point[1]
                positions.append((line, char))
        for child in n.children:
            visit(child)

    visit(tree.root_node)
    if not positions:
        return []

    try:
        from src.mcp_server.config import settings
        timeout = getattr(settings, 'lsp_request_timeout', 3.0)
    except Exception:
        timeout = 3.0

    if hasattr(lsp_client, 'resolve_definitions_batch'):
        return lsp_client.resolve_definitions_batch(file_str, positions, timeout_per_query=timeout, max_queries=50)

    resolutions = []
    for line, char in positions[:50]:
        try:
            res = lsp_client.resolve_definition(file_str, line, char, timeout=timeout)
            if res:
                resolutions.append({
                    "call_line": line,
                    "def_filepath": res["filepath"],
                    "def_line": res["start_line"]
                })
        except Exception as e:
            logger.debug("Failed to resolve definition: %s", e)
    return resolutions


def link_lsp_call_edges(ram_search, ram_graph, all_lsp_resolutions: list) -> None:
    """Link cross-file CALLS edges resolved via LSP during Phase 1."""
    if not all_lsp_resolutions:
        return
    try:
        db_nodes = ram_search._conn.execute(
            "SELECT id, filepath, start_line, end_line, kind FROM code_nodes "
            "WHERE LOWER(kind) IN ('method', 'function', 'class')"
        ).fetchall()
        db_file_index = defaultdict(list)
        for nid, fp, s_line, e_line, kind in db_nodes:
            lbl = (kind or "").capitalize()
            db_file_index[fp].append((s_line or 0, e_line or 0, nid, lbl))

        for fp in db_file_index:
            db_file_index[fp].sort()

        def _find_containing_in_db(filepath, line, kinds):
            for start, end, nid, lbl in db_file_index.get(filepath, []):
                if lbl in kinds and start <= line <= end:
                    return nid
            return None

        lsp_calls_to_insert = set()
        for file_str, resolutions in all_lsp_resolutions:
            for resolution in resolutions:
                call_line = resolution["call_line"]
                def_filepath = resolution["def_filepath"]
                def_line = resolution["def_line"]

                caller_id = _find_containing_in_db(file_str, call_line, ("Method", "Function"))
                callee_id = _find_containing_in_db(def_filepath, def_line, ("Method", "Function", "Class"))

                if caller_id and callee_id:
                    lsp_calls_to_insert.add((caller_id, callee_id, frozenset(), "CALLS"))

        if lsp_calls_to_insert:
            lsp_edges_list = [(src, dst, dict(props), rel) for src, dst, props, rel in lsp_calls_to_insert]
            ram_graph.insert_edges_bulk(lsp_edges_list)
    except Exception as e:
        logger.warning("Failed during Phase 2 LSP call resolution: %s", e)
