import csv
import json
import logging
import os
import tempfile
import traceback
from typing import Any, Callable, Optional

from src.mcp_server.indexing.git_analytics import compute_git_features
from src.mcp_server.naming_analyzer import tokenize_identifier

logger = logging.getLogger(__name__)


def compute_similarity_edges(search_index, graph, enable_embeddings: bool = True) -> None:
    """Compute SIMILAR_TO (MinHash/Jaccard) and SEMANTICALLY_RELATED (Vector) edges."""
    logger.info("Computing similarity edges (SIMILAR_TO, SEMANTICALLY_RELATED)...")
    try:
        # 1. SEMANTICALLY_RELATED (Vector Similarity)
        if enable_embeddings and search_index:
            try:
                rows = search_index._conn.execute(
                    "SELECT id, filepath, embedding FROM code_nodes "
                    "WHERE LOWER(kind) IN ('function', 'method', 'class') AND embedding IS NOT NULL"
                ).fetchall()
                if rows and len(rows) > 1:
                    import numpy as np
                    valid_nodes = []
                    vecs = []
                    for r in rows:
                        if not r[2]:
                            continue
                        try:
                            emb = json.loads(r[2]) if isinstance(r[2], str) else r[2]
                            if emb and isinstance(emb, (list, tuple)):
                                valid_nodes.append((r[0], r[1]))
                                vecs.append(emb)
                        except Exception:
                            continue

                    if len(vecs) > 1:
                        mat = np.array(vecs, dtype=np.float32)
                        norms = np.linalg.norm(mat, axis=1, keepdims=True)
                        norms[norms == 0] = 1.0
                        normalized = mat / norms
                        sim = np.dot(normalized, normalized.T)

                        semantic_edges = []
                        for i in range(len(valid_nodes)):
                            for j in range(i + 1, len(valid_nodes)):
                                if valid_nodes[i][1] != valid_nodes[j][1] and sim[i, j] >= 0.80:
                                    score = float(sim[i, j])
                                    semantic_edges.extend([
                                        (valid_nodes[i][0], valid_nodes[j][0], score),
                                        (valid_nodes[j][0], valid_nodes[i][0], score)
                                    ])

                        if semantic_edges:
                            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as f:
                                writer = csv.writer(f)
                                for edge in semantic_edges:
                                    writer.writerow(edge)
                                tmp_path = f.name

                            try:
                                with graph:
                                    graph._conn.execute(f"COPY SEMANTICALLY_RELATED FROM '{tmp_path}' (HEADER=false);")
                                logger.info(f"Loaded {len(semantic_edges)} SEMANTICALLY_RELATED edges.")
                            finally:
                                if os.path.exists(tmp_path):
                                    os.remove(tmp_path)
            except Exception as e:
                logger.warning(f"Failed to generate SEMANTICALLY_RELATED edges: {e}")

        # 2. SIMILAR_TO (MinHash/Jaccard via datasketch)
        if search_index:
            try:
                from datasketch import MinHash, MinHashLSH
                text_query = (
                    "SELECT id, name, filepath, start_line, end_line, start_byte, end_byte "
                    "FROM code_nodes WHERE LOWER(kind) IN ('function', 'method', 'class')"
                )
                rows = search_index._conn.execute(text_query).fetchall()

                if rows:
                    lsh = MinHashLSH(threshold=0.70, num_perm=128)
                    minhashes = {}

                    for nid, name, filepath, start_line, end_line, start_byte, end_byte in rows:
                        body = ""
                        if filepath and hasattr(search_index, "_lazy_load_body"):
                            body = search_index._lazy_load_body(
                                filepath,
                                start_line or 0,
                                end_line or 0,
                                start_byte or 0,
                                end_byte or 0,
                            )
                        content = str(body or "").strip()
                        if content:
                            # Truncate to 4KB — beyond this, MinHash similarity stabilizes
                            content = content[:4096]
                            # Word-level tokens: much fewer update() calls than char 3-grams
                            tokens = content.split()
                        else:
                            tokens = tokenize_identifier(str(name or ""))

                        if len(tokens) < 3:
                            continue
                        m = MinHash(num_perm=128)
                        for d in tokens:
                            m.update(d.encode('utf8'))
                        lsh.insert(nid, m)
                        minhashes[nid] = m

                    similar_edges = []
                    seen_pairs = set()
                    for nid, m in minhashes.items():
                        result = lsh.query(m)
                        for r_id in result:
                            if nid != r_id and (r_id, nid) not in seen_pairs:
                                jaccard = m.jaccard(minhashes[r_id])
                                if jaccard >= 0.70:
                                    similar_edges.extend([
                                        (nid, r_id, float(jaccard)),
                                        (r_id, nid, float(jaccard))
                                    ])
                                    seen_pairs.add((nid, r_id))

                    if similar_edges:
                        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as f:
                            writer = csv.writer(f)
                            for edge in similar_edges:
                                writer.writerow(edge)
                            tmp_path = f.name

                        try:
                            with graph:
                                graph._conn.execute(f"COPY SIMILAR_TO FROM '{tmp_path}' (HEADER=false);")
                            logger.info(f"Loaded {len(similar_edges)} SIMILAR_TO edges.")
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)

            except ImportError:
                logger.warning("datasketch not installed. Skipping SIMILAR_TO edge generation.")

    except Exception as e:
        logger.warning(f"Failed to compute similarity edges: {e}")
        logger.debug(traceback.format_exc())


def post_process_graph(
    search_index,
    graph,
    repo_path: str,
    enable_embeddings: bool = True,
    compute_git_features_fn: Optional[Callable[[str], list]] = None,
) -> None:
    """Find recursive self-calls and resolve Symbol/External nodes to Method/Function for dynamic languages."""
    compute_similarity_edges(search_index, graph, enable_embeddings=enable_embeddings)

    queries = [
        "MATCH (m:CodeNode {kind: 'Method'})-[r:RECURSES_TO]->(m) DELETE r",
        "MATCH (f:CodeNode {kind: 'Function'})-[r:RECURSES_TO]->(f) DELETE r",
        "MATCH (m:CodeNode {kind: 'Method'})-[:CALLS]->(m) CREATE (m)-[:RECURSES_TO]->(m)",
        "MATCH (f:CodeNode {kind: 'Function'})-[:CALLS]->(f) CREATE (f)-[:RECURSES_TO]->(f)",
        # Resolve CALLS to Symbol/External nodes into direct CALLS to Method/Function nodes.
        # Handle dotted attribute access: 'self.rebuild_fts' → match Method named 'rebuild_fts'
        # Handle class-qualified: 'search_index.rebuild_fts' → match 'rebuild_fts'
        # Method callers → Method targets
        "MATCH (caller:CodeNode {kind: 'Method'})-[:CALLS]->(s:CodeNode), (m:CodeNode {kind: 'Method'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = m.name OR ends_with(s.name, '.' + m.name)) CREATE (caller)-[:CALLS]->(m)",
        # Method callers → Function targets
        "MATCH (caller:CodeNode {kind: 'Method'})-[:CALLS]->(s:CodeNode), (f:CodeNode {kind: 'Function'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = f.name OR ends_with(s.name, '.' + f.name)) CREATE (caller)-[:CALLS]->(f)",
        # Function callers → Method targets
        "MATCH (caller:CodeNode {kind: 'Function'})-[:CALLS]->(s:CodeNode), (m:CodeNode {kind: 'Method'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = m.name OR ends_with(s.name, '.' + m.name)) CREATE (caller)-[:CALLS]->(m)",
        # Function callers → Function targets
        "MATCH (caller:CodeNode {kind: 'Function'})-[:CALLS]->(s:CodeNode), (f:CodeNode {kind: 'Function'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = f.name OR ends_with(s.name, '.' + f.name)) CREATE (caller)-[:CALLS]->(f)",
        # ControlFlow callers → Method/Function targets
        "MATCH (caller:CodeNode {kind: 'ControlFlow'})-[:CALLS]->(s:CodeNode), (m:CodeNode {kind: 'Method'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = m.name OR ends_with(s.name, '.' + m.name)) CREATE (caller)-[:CALLS]->(m)",
        "MATCH (caller:CodeNode {kind: 'ControlFlow'})-[:CALLS]->(s:CodeNode), (f:CodeNode {kind: 'Function'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = f.name OR ends_with(s.name, '.' + f.name)) CREATE (caller)-[:CALLS]->(f)",
        # Lambda callers → Method/Function targets
        "MATCH (caller:CodeNode {kind: 'Lambda'})-[:CALLS]->(s:CodeNode), (m:CodeNode {kind: 'Method'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = m.name OR ends_with(s.name, '.' + m.name)) CREATE (caller)-[:CALLS]->(m)",
        "MATCH (caller:CodeNode {kind: 'Lambda'})-[:CALLS]->(s:CodeNode), (f:CodeNode {kind: 'Function'}) WHERE s.kind IN ['Symbol', 'External'] AND (s.name = f.name OR ends_with(s.name, '.' + f.name)) CREATE (caller)-[:CALLS]->(f)",
    ]
    try:
        with graph:
            graph.query_batch(queries)

        # Log resolution stats
        try:
            with graph:
                total_calls = graph.query("MATCH ()-[r:CALLS]->() RETURN count(r) AS cnt")
                to_symbol = graph.query("MATCH ()-[:CALLS]->(s:CodeNode) WHERE s.kind IN ['Symbol', 'External'] RETURN count(s) AS cnt")
                to_resolved = graph.query("MATCH ()-[:CALLS]->(t:CodeNode) WHERE NOT t.kind IN ['Symbol', 'External'] RETURN count(t) AS cnt")
                total = total_calls[0].get('cnt', 0) if total_calls else 0
                sym = to_symbol[0].get('cnt', 0) if to_symbol else 0
                res = to_resolved[0].get('cnt', 0) if to_resolved else 0
                logger.info("Symbol resolution: %d total CALLS, %d resolved, %d still pointing to Symbol/External nodes", total, res, sym)
        except Exception as e:
            logger.debug("Failed to get resolution stats: %s", e)

        # After graph relationships are resolved, calculate and build PageRank
        try:
            pr_scores = graph.pagerank()
            if pr_scores:
                search_index.update_pagerank_bulk(pr_scores)
        except Exception as e:
            logger.warning("Failed to calculate or build PageRank: %s", e)

        # Compute in/out degree from CALLS edges
        try:
            with graph:
                out_rows = graph.query(
                    "MATCH (n)-[:CALLS]->(t:CodeNode) WHERE NOT t.kind IN ['Symbol', 'External'] "
                    "RETURN n.name AS name, count(t) AS deg"
                )
                in_rows = graph.query(
                    "MATCH (s:CodeNode)-[:CALLS]->(n) WHERE NOT s.kind IN ['Symbol', 'External'] "
                    "RETURN n.name AS name, count(s) AS deg"
                )
            degree_map = {}
            for row in out_rows:
                name = row.get("name", "")
                degree_map.setdefault(name, {"name": name, "in_degree": 0, "out_degree": 0})
                degree_map[name]["out_degree"] = row.get("deg", 0)
            for row in in_rows:
                name = row.get("name", "")
                degree_map.setdefault(name, {"name": name, "in_degree": 0, "out_degree": 0})
                degree_map[name]["in_degree"] = row.get("deg", 0)
            if degree_map:
                search_index.update_degrees_bulk(list(degree_map.values()))
        except Exception as e:
            logger.warning("Failed to compute in/out degree: %s", e)

        # Leiden Sweep for community detection
        try:
            from src.mcp_server import graph_algorithms
            logger.info("Starting Leiden sweep for community detection...")

            with graph:
                try:
                    graph._conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                except Exception as e:
                    logger.debug("Failed to drop CodeGraph before projection: %s", e)
                graph._conn.execute("""
                    CALL PROJECT_GRAPH('CodeGraph',
                        ['File', 'CodeNode', 'Identifier'],
                        ['DEPENDS_ON', 'CONTAINS', 'DEFINES', 'INHERITS', 'IMPLEMENTS', 'CALLS', 'FILE_CHANGES_WITH', 'RAISES', 'TESTS', 'HTTP_CALLS', 'IMPORTS', 'READS', 'WRITES', 'RETURNS', 'HAS_PARAMETER', 'USES']
                    );
                """)

                sweep_results = graph_algorithms.sweep_gamma(graph, graph_name='CodeGraph')
                stable_regions = graph_algorithms.find_stable_partition(sweep_results)
                best_partition_info = graph_algorithms.get_best_partition(stable_regions)

                if best_partition_info:
                    partition_dict = best_partition_info["partition"]
                    community_updates = [{"node_id": k, "community_id": v} for k, v in partition_dict.items()]
                    search_index.update_community_bulk(community_updates)
                    logger.info("Successfully updated community IDs based on best partition (gamma=%.2f)", best_partition_info["gamma_begin"])
                else:
                    logger.warning("No stable partition found from Leiden sweep.")

                try:
                    graph._conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                except Exception as e:
                    logger.debug("Failed to drop CodeGraph after sweep: %s", e)
        except Exception as e:
            logger.warning("Failed to run Leiden sweep: %s", e)
            logger.debug(traceback.format_exc())

        # Compute and bulk update Git temporal & stable features
        try:
            git_fn = compute_git_features_fn or compute_git_features
            git_feats = git_fn(repo_path)
            if git_feats:
                search_index.update_git_features_bulk(git_feats)
        except Exception as e:
            logger.warning("Failed to compute or update git features: %s", e)

        # Calculate and bulk update Object-Oriented Design (OOD) Features
        try:
            ood_map: dict[str, dict[str, Any]] = {}

            # 1. Instability and Coupling from in_degree / out_degree
            node_rows = search_index._conn.execute(
                "SELECT id, in_degree, out_degree FROM code_nodes"
            ).fetchall()
            for nid, in_deg, out_deg in node_rows:
                in_d = float(in_deg or 0)
                out_d = float(out_deg or 0)
                coupling = in_d + out_d
                instability = out_d / coupling if coupling > 0 else 0.0
                ood_map[nid] = {
                    "id": nid,
                    "instability": round(instability, 4),
                    "coupling": coupling,
                    "depth": 0,
                    "inheritance_depth": 0,
                    "betweenness": 0.0,
                }

            # 2. Inheritance Depth, Containment Depth, and Betweenness from Graph
            try:
                with graph:
                    inherit_rows = graph.query(
                        "MATCH (c:CodeNode)-[:INHERITS*1..10]->(a:CodeNode) "
                        "RETURN c.id AS id, count(a) AS depth"
                    )
                    for row in inherit_rows:
                        nid = row.get("id")
                        if nid in ood_map:
                            ood_map[nid]["inheritance_depth"] = int(row.get("depth", 0))

                    contain_rows = graph.query(
                        "MATCH (parent:CodeNode)-[:CONTAINS*1..10]->(child:CodeNode) "
                        "RETURN child.id AS id, count(parent) AS depth"
                    )
                    for row in contain_rows:
                        nid = row.get("id")
                        if nid in ood_map:
                            ood_map[nid]["depth"] = int(row.get("depth", 0))

                    between_rows = graph.query(
                        "MATCH (a:CodeNode)-[:CALLS]->(n:CodeNode)-[:CALLS]->(b:CodeNode) "
                        "WHERE a.id <> b.id "
                        "RETURN n.id AS id, count(*) AS bridges"
                    )
                    max_bridges = max([r.get("bridges", 0) for r in between_rows], default=1) or 1
                    for row in between_rows:
                        nid = row.get("id")
                        if nid in ood_map:
                            ood_map[nid]["betweenness"] = round(float(row.get("bridges", 0)) / max_bridges, 4)
            except Exception as e:
                logger.debug("Failed graph query for OOD features: %s", e)

            if ood_map:
                search_index.update_ood_features_bulk(list(ood_map.values()))
        except Exception as e:
            logger.warning("Failed to compute or update OOD features: %s", e)

    except Exception as e:
        logger.warning("Failed to post-process graph: %s", e)
        logger.debug(traceback.format_exc())


def run_hcgs_pass(
    graph,
    search_index,
    embedder=None,
    enable_embeddings: bool = True,
    progress_callback=None,
    total_files: int = 0,
) -> None:
    """Run static HCGS pass (bottom-up context propagation and re-embedding)."""
    try:
        if progress_callback:
            progress_callback(total_files, total_files, "Generating static HCGS summaries...")
        from src.mcp_server.hcgs import build_levels, process_levels_static
        levels = build_levels(graph)
        if levels:
            summaries = process_levels_static(levels, graph, search_index._conn)
            if summaries:
                search_index.update_summaries_bulk(summaries)
                if enable_embeddings and embedder:
                    if progress_callback:
                        progress_callback(total_files, total_files, f"Embedding {len(summaries)} static HCGS summaries...")
                    summary_texts = list(summaries.values())
                    summary_ids = list(summaries.keys())
                    embeddings = embedder.embed_texts(summary_texts)
                    if embeddings:
                        pairs = list(zip(summary_ids, embeddings))
                        search_index.update_embeddings_bulk(pairs)
    except Exception as e:
        logger.warning("Failed to process static HCGS summaries: %s", e)
        logger.debug(traceback.format_exc())


def verify_index_integrity(search_index, graph) -> dict:
    """Post-indexing sanity check: verify SQLite ↔ Gorgonzola consistency.

    Returns a dict with integrity stats and any warnings found.
    Non-blocking: logs warnings but never raises.
    """
    warnings = []
    stats = {}
    try:
        # 1. Count SQLite files vs Graph File nodes
        file_count = search_index._conn.execute(
            "SELECT count(*) FROM files"
        ).fetchone()[0]
        stats["sqlite_files"] = file_count
        stats["duckdb_files"] = file_count  # Compatibility alias

        try:
            with graph:
                graph_file_rows = graph.query(
                    "MATCH (f:CodeNode {kind: 'File'}) RETURN count(f) AS cnt"
                )
                graph_file_count = graph_file_rows[0].get("cnt", 0) if graph_file_rows else 0
                stats["graph_file_nodes"] = graph_file_count

                if file_count > 0 and graph_file_count == 0:
                    warnings.append(
                        f"Graph has 0 File nodes but SQLite has {file_count} files — "
                        "graph may be empty or corrupted"
                    )
                elif file_count > 0 and graph_file_count < file_count:
                    warnings.append(
                        f"File count mismatch: Graph ({graph_file_count}) has fewer files than SQLite ({file_count}) — "
                        "stores may be out of sync"
                    )

                # 2. Count SQLite code_nodes vs Graph Function/Method/Class nodes
                symbol_count = search_index._conn.execute(
                    "SELECT count(*) FROM code_nodes"
                ).fetchone()[0]
                stats["sqlite_symbols"] = symbol_count
                stats["duckdb_symbols"] = symbol_count  # Compatibility alias

                graph_symbol_rows = graph.query(
                    "MATCH (n:CodeNode) WHERE n.kind IN ['Function', 'Method', 'Class'] "
                    "RETURN count(n) AS cnt"
                )
                graph_symbol_count = graph_symbol_rows[0].get("cnt", 0) if graph_symbol_rows else 0
                stats["graph_symbols"] = graph_symbol_count

                # 3. Check for orphan edges (edges referencing unresolved Symbol/External nodes)
                orphan_rows = graph.query(
                    "MATCH (a)-[r:CALLS]->(b:CodeNode) "
                    "WHERE b.kind IN ['Symbol', 'External'] "
                    "AND NOT EXISTS { MATCH (t:CodeNode) WHERE t.name = b.name AND NOT t.kind IN ['Symbol', 'External'] } "
                    "RETURN count(r) AS cnt"
                )
                unresolved_symbols = orphan_rows[0].get("cnt", 0) if orphan_rows else 0
                stats["unresolved_symbol_edges"] = unresolved_symbols

        except Exception as e:
            warnings.append(f"Graph integrity check failed: {e}")

    except Exception as e:
        warnings.append(f"SQLite integrity check failed: {e}")

    if warnings:
        for w in warnings:
            logger.warning("[integrity] %s", w)
        stats["warnings"] = warnings
    else:
        logger.info("[integrity] Post-index verification passed: %s", stats)

    return stats
