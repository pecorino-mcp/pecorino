import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

def sweep_gamma(graph, gammas: List[float] = None, graph_name: str = 'g') -> List[Dict[str, Any]]:
    """
    Run CALL LEIDEN(..., quality:='cpm', gamma:=γ) for each γ.
    Returns a list of partitions across resolution parameters.
    """
    if gammas is None:
        gammas = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 0.80, 1.0, 1.5, 2.0, 3.0]

    results = []
    for gamma in gammas:
        try:
            # Execute Native Leiden with CPM and given gamma
            query = (
                f"CALL LEIDEN('{graph_name}', quality:='cpm', gamma:={gamma}, seed:=42) "
                f"RETURN id(node) AS node_id, leiden_id"
            )
            # Depending on Gorgonzola's python API, execute may return rows
            rows = graph.query(query)

            partition = {}
            for r in rows:
                # Handle varying dictionary/tuple return shapes from duckdb/gorgonzola
                if isinstance(r, dict):
                    nid = r.get("node_id")
                    lid = r.get("leiden_id")
                else:
                    nid = r[0]
                    lid = r[1]

                # In Gorgonzola, internal node IDs are often returned as dicts like {'offset': 0, 'table': 0}
                if isinstance(nid, dict):
                    # Sort keys to ensure deterministic string representation
                    nid = str(sorted(nid.items()))

                partition[nid] = lid

            results.append({"gamma": gamma, "partition": partition})
            logger.info(f"Leiden Sweep: Gamma={gamma} yielded {len(set(partition.values()))} communities.")
        except Exception as e:
            logger.warning(f"Leiden Sweep failed for Gamma={gamma}: {e}")

    return results

def adjusted_rand_index(p1: Dict[Any, int], p2: Dict[Any, int]) -> float:
    """
    Compute Adjusted Rand Index (ARI) between two community partitions.
    O(n) complexity using contingency tables.
    """
    # Find overlapping nodes
    nodes = set(p1.keys()).intersection(set(p2.keys()))
    if not nodes:
        return 0.0

    # Build contingency table
    contingency = {}
    sum_i = {}
    sum_j = {}

    for n in nodes:
        c1, c2 = p1[n], p2[n]
        if c1 not in contingency:
            contingency[c1] = {}
        contingency[c1][c2] = contingency[c1].get(c2, 0) + 1
        sum_i[c1] = sum_i.get(c1, 0) + 1
        sum_j[c2] = sum_j.get(c2, 0) + 1

    n_nodes = len(nodes)

    # Calculate combinations (x choose 2)
    def c2(x): return x * (x - 1) / 2.0

    sum_n_ij = sum(c2(count) for row in contingency.values() for count in row.values())
    sum_a = sum(c2(count) for count in sum_i.values())
    sum_b = sum(c2(count) for count in sum_j.values())

    expected_index = (sum_a * sum_b) / c2(n_nodes) if n_nodes > 1 else 0
    max_index = (sum_a + sum_b) / 2.0

    if max_index == expected_index:
        return 1.0

    ari = (sum_n_ij - expected_index) / (max_index - expected_index)
    return ari

def find_stable_partition(partitions: List[Dict[str, Any]], threshold: float = 0.99) -> List[Dict[str, Any]]:
    """
    Find the gamma intervals where ARI > threshold for consecutive γ values.
    Returns stable regions with their metrics.
    """
    stable_regions = []
    for i in range(len(partitions) - 1):
        p1 = partitions[i]["partition"]
        p2 = partitions[i+1]["partition"]

        ari = adjusted_rand_index(p1, p2)
        if ari > threshold:
            stable_regions.append({
                "gamma_begin": partitions[i]["gamma"],
                "gamma_end": partitions[i+1]["gamma"],
                "ari": ari,
                "partition": p1,
                "community_count": len(set(p1.values()))
            })

    return stable_regions

def get_best_partition(stable_regions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Selects the best partition from stable regions.
    Currently defaults to the partition that is most stable (or falls back).
    """
    if not stable_regions:
        return None

    # Heuristic: choose the one with the highest ARI stability, breaking ties by wider gamma range if we tracked it,
    # or just picking the middle complexity one. For now, we take the one with highest ARI.
    return sorted(stable_regions, key=lambda x: x["ari"], reverse=True)[0]


def compute_ppr_scores(graph, seed_scores: Dict[str, float], alpha: float = 0.15, n_iter: int = 10) -> Dict[str, float]:
    """
    Personalized PageRank (PPR) on candidate seed nodes over a 2-hop truncated subgraph.
    
    Args:
        graph: GorgonzolaGraph instance
        seed_scores: Dict mapping node_id -> initial score
        alpha: Teleport probability (default 0.15)
        n_iter: Number of power iterations (default 10)
        
    Returns:
        Dict mapping node_id -> normalized PPR score
    """
    if not seed_scores or not graph:
        return {}

    seed_ids = list(seed_scores.keys())
    if not seed_ids:
        return {}

    total_seed_score = sum(seed_scores.values()) or 1.0
    p0 = {nid: score / total_seed_score for nid, score in seed_scores.items()}

    adj = {}
    try:
        # Extract 1-hop and 2-hop neighborhood for candidate seeds to build truncated subgraph
        query = (
            "MATCH (a:CodeNode)-[r]->(b:CodeNode) "
            "WHERE a.id IN $ids "
            "RETURN a.id AS src, b.id AS dst"
        )
        rows = graph.query(query, {"ids": seed_ids})
        hop1_dsts = set()
        for r in rows:
            src = r.get("src") if isinstance(r, dict) else r[0]
            dst = r.get("dst") if isinstance(r, dict) else r[1]
            if src and dst:
                adj.setdefault(src, set()).add(dst)
                hop1_dsts.add(dst)

        # 2-hop expansion from 1-hop neighbors (capped to keep subgraph < 2000 nodes)
        if hop1_dsts and len(hop1_dsts) < 1500:
            query_2hop = (
                "MATCH (a:CodeNode)-[r]->(b:CodeNode) "
                "WHERE a.id IN $ids "
                "RETURN a.id AS src, b.id AS dst"
            )
            rows_2hop = graph.query(query_2hop, {"ids": list(hop1_dsts)[:500]})
            for r in rows_2hop:
                src = r.get("src") if isinstance(r, dict) else r[0]
                dst = r.get("dst") if isinstance(r, dict) else r[1]
                if src and dst:
                    adj.setdefault(src, set()).add(dst)
    except Exception as e:
        logger.debug(f"PPR subgraph query failed: {e}")

    p = dict(p0)
    all_nodes = set(p0.keys()).union(adj.keys())

    for _ in range(n_iter):
        p_next = {nid: (alpha * p0.get(nid, 0.0)) for nid in all_nodes}
        for u, neighbors in adj.items():
            if not neighbors:
                continue
            prob = (1.0 - alpha) * p.get(u, 0.0) / len(neighbors)
            for v in neighbors:
                p_next[v] = p_next.get(v, 0.0) + prob
        p = p_next

    max_p = max(p.values()) if p else 1.0
    if max_p > 0:
        return {nid: score / max_p for nid, score in p.items() if score > 0}
    return p


def compute_prone_embeddings(graph, dim: int = 64) -> Dict[str, List[int]]:
    """
    Compute ProNE structural embeddings (64d int8) for graph nodes.
    
    Step 1: Sparse Matrix Factorization (Truncated SVD).
    Step 2: Spectral propagation via Chebyshev polynomials over graph manifold.
    
    Args:
        graph: GorgonzolaGraph instance
        dim: Embedding dimension (default 64)
        
    Returns:
        Dict mapping node_id -> list of 64 int8 values
    """
    if not graph:
        return {}

    try:
        query = "MATCH (a:CodeNode)-[r]->(b:CodeNode) RETURN a.id AS src, b.id AS dst"
        rows = graph.query(query)
        if not rows:
            return {}

        import numpy as np
        nodes_set = set()
        edges = []
        for r in rows:
            src = r.get("src") if isinstance(r, dict) else r[0]
            dst = r.get("dst") if isinstance(r, dict) else r[1]
            if src and dst:
                nodes_set.add(src)
                nodes_set.add(dst)
                edges.append((src, dst))

        node_list = sorted(list(nodes_set))
        node_to_idx = {nid: i for i, nid in enumerate(node_list)}
        n = len(node_list)
        if n < 2:
            return {}

        try:
            import scipy.sparse as sp
            from scipy.sparse.linalg import svds

            row_indices = [node_to_idx[src] for src, dst in edges]
            col_indices = [node_to_idx[dst] for src, dst in edges]
            data = np.ones(len(edges), dtype=np.float32)

            adj = sp.csr_matrix((data, (row_indices, col_indices)), shape=(n, n))
            adj = adj + adj.T

            # Step 1: Truncated SVD
            k = min(dim, n - 1)
            u, s, vt = svds(adj, k=k)
            emb_matrix = u * np.sqrt(s)

            if emb_matrix.shape[1] < dim:
                pad_width = ((0, 0), (0, dim - emb_matrix.shape[1]))
                emb_matrix = np.pad(emb_matrix, pad_width, mode='constant')

            # Step 2: Chebyshev Spectral Propagation (ProNE higher-order smoothing)
            deg = np.array(adj.sum(axis=1)).flatten()
            deg[deg == 0] = 1.0
            d_inv_sqrt = sp.diags(1.0 / np.sqrt(deg))
            norm_adj = d_inv_sqrt.dot(adj).dot(d_inv_sqrt)

            mu1 = norm_adj.dot(emb_matrix)
            mu2 = norm_adj.dot(mu1)
            emb_matrix = 0.5 * emb_matrix + 0.3 * mu1 + 0.2 * mu2

            # Quantize to INT8 (-128 to 127)
            max_val = np.max(np.abs(emb_matrix), axis=1, keepdims=True)
            max_val[max_val == 0] = 1.0
            quantized = np.clip(np.round((emb_matrix / max_val) * 127.0), -128, 127).astype(np.int8)

            result = {}
            for i, nid in enumerate(node_list):
                result[nid] = quantized[i].tolist()
            return result
        except Exception as e:
            logger.warning(f"Spectral/ProNE computation failed: {e}")
            return {}
    except Exception as e:
        logger.warning(f"Failed to compute ProNE embeddings: {e}")
        return {}



