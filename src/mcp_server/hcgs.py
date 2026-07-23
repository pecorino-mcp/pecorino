import logging
from collections import defaultdict, deque
from typing import Dict, List, Set, Any, Tuple, Optional

logger = logging.getLogger(__name__)

def build_levels(graph) -> List[List[str]]:
    """
    Query CALLS edges from the Kùzu graph and organize Function/Method nodes into dependency levels.
    
    Level 0 contains leaf functions (functions that make no outgoing calls to other indexed functions).
    Level 1 contains functions that only call Level 0 functions.
    Cycle breaking is performed if cyclic call dependencies exist.
    """
    query = """
    MATCH (caller:CodeNode)-[:CALLS]->(callee:CodeNode)
    WHERE caller.kind IN ['Function', 'Method'] AND callee.kind IN ['Function', 'Method']
    RETURN caller.id AS caller_id, callee.id AS callee_id
    """
    
    edges: List[Tuple[str, str]] = []
    nodes: Set[str] = set()
    
    try:
        with graph:
            rows = graph.query(query)
            for row in rows:
                c_id = row.get("caller_id")
                e_id = row.get("callee_id")
                if c_id and e_id:
                    edges.append((c_id, e_id))
                    nodes.add(c_id)
                    nodes.add(e_id)
    except Exception as e:
        logger.warning("Failed to query CALLS edges for level construction: %s", e)
        return []

    if not nodes:
        return []

    # Outgoing dependencies map: node -> set of callees
    callees_map: Dict[str, Set[str]] = defaultdict(set)
    # Incoming callers map: node -> set of callers
    callers_map: Dict[str, Set[str]] = defaultdict(set)

    for caller, callee in edges:
        if caller != callee:  # Ignore self-recursion for level order
            callees_map[caller].add(callee)
            callers_map[callee].add(caller)

    remaining_nodes = set(nodes)
    processed_nodes: Set[str] = set()
    levels: List[List[str]] = []

    while remaining_nodes:
        # Find nodes whose callees have all been processed
        current_level = [
            n for n in remaining_nodes
            if callees_map[n].issubset(processed_nodes)
        ]

        if not current_level:
            # Cycle detected: pick the node with the fewest remaining unprocessed callees
            min_callees_node = min(
                remaining_nodes,
                key=lambda n: len(callees_map[n] - processed_nodes)
            )
            current_level = [min_callees_node]

        levels.append(current_level)
        for n in current_level:
            processed_nodes.add(n)
            remaining_nodes.remove(n)

    return levels

def build_static_summary(
    node_id: str,
    name: str,
    kind: str,
    docstring: str = "",
    signature: str = "",
    parameters: Optional[List[Dict[str, Any]]] = None,
    return_type: str = "",
    called_summaries: Optional[List[Tuple[str, str]]] = None, # [(callee_name, callee_short_summary)]
    reads: Optional[List[str]] = None,
    writes: Optional[List[str]] = None,
    complexity: int = 0,
) -> str:
    """
    Assemble a structured text summary from AST metadata without calling an LLM.
    """
    parts = []

    # 1. Header & Signature
    clean_name = name.split(".")[-1] if "." in name else name
    parts.append(f"{kind} {clean_name}.")
    
    if docstring:
        clean_doc = docstring.strip().replace("\n", " ")
        if len(clean_doc) > 200:
            clean_doc = clean_doc[:197] + "..."
        parts.append(f"Purpose: {clean_doc}")
    elif signature:
        parts.append(f"Signature: {signature}")

    # 2. Parameters & Return
    if parameters:
        param_strs = []
        for p in parameters:
            p_name = p.get("name", "")
            p_type = p.get("type", "")
            if p_type:
                param_strs.append(f"{p_name}: {p_type}")
            elif p_name:
                param_strs.append(p_name)
        if param_strs:
            parts.append(f"Parameters: {', '.join(param_strs)}.")

    if return_type:
        parts.append(f"Returns: {return_type}.")

    # 3. Bottom-up Context (Called Functions & Summaries)
    if called_summaries:
        call_descriptions = []
        for c_name, c_sum in called_summaries[:5]: # Limit to top 5 callees to prevent token blowout
            if c_sum:
                # Use first line or short snippet of child summary
                short_child = c_sum.split(".")[0]
                call_descriptions.append(f"{c_name} ({short_child})")
            else:
                call_descriptions.append(c_name)
        parts.append(f"Calls: {', '.join(call_descriptions)}.")

    # 4. State Accesses (Side Effects)
    if reads:
        parts.append(f"Reads state: {', '.join(reads[:5])}.")
    if writes:
        parts.append(f"Mutates state: {', '.join(writes[:5])}.")

    if complexity > 0:
        parts.append(f"Complexity score: {complexity}.")

    return " ".join(parts)

def process_levels_static(
    levels: List[List[str]],
    graph,
    db_conn,
    max_depth: int = 2
) -> Dict[str, str]:
    """
    Process levels bottom-up to build static summaries with propagated context.
    Returns a dict mapping node_id -> static_summary_text.
    """
    summaries: Dict[str, str] = {}
    
    if not levels:
        return summaries

    # Pre-fetch all symbol properties from DuckDB for fast lookup
    rows = db_conn.execute(
        "SELECT id, name, kind, signature, complexity, relationships FROM code_nodes"
    ).fetchall()
    
    node_props: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        node_props[r[0]] = {
            "name": r[1],
            "kind": r[2],
            "signature": r[3],
            "complexity": r[4],
            "relationships": r[5]
        }

    # Fetch docstrings & called child relationships from graph
    with graph:
        # Pre-fetch child relationships (callees) for all nodes
        callee_rel_query = """
        MATCH (caller:CodeNode)-[:CALLS]->(callee:CodeNode)
        WHERE caller.kind IN ['Function', 'Method']
        RETURN caller.id AS caller_id, callee.id AS callee_id, callee.name AS callee_name
        """
        child_map: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        try:
            rel_rows = graph.query(callee_rel_query)
            for r in rel_rows:
                child_map[r["caller_id"]].append((r["callee_id"], r["callee_name"]))
        except Exception as e:
            logger.warning("Failed to fetch callee relationships for HCGS: %s", e)

        # Process bottom-up level by level
        for level in levels:
            for node_id in level:
                props = node_props.get(node_id, {})
                name = props.get("name", node_id.split("::")[-2] if "::" in node_id else node_id)
                kind = props.get("kind", "Function")
                sig = props.get("signature", "")
                comp = props.get("complexity", 0)

                # Gather child summaries
                called_summaries: List[Tuple[str, str]] = []
                for child_id, child_name in child_map.get(node_id, []):
                    c_sum = summaries.get(child_id, "")
                    called_summaries.append((child_name, c_sum))

                # Assemble static summary
                summary_text = build_static_summary(
                    node_id=node_id,
                    name=name,
                    kind=kind,
                    signature=sig,
                    called_summaries=called_summaries,
                    complexity=comp
                )

                summaries[node_id] = summary_text

    return summaries
