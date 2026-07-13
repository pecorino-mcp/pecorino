import logging
from typing import Dict, List, Any

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
