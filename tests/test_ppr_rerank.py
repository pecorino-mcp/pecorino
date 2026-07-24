import pytest
from src.mcp_server.graph_algorithms import compute_ppr_scores

class MockGraph:
    def __init__(self, edges):
        self.edges = edges  # [(src, dst)]

    def query(self, cypher_query, params=None):
        ids = set(params.get("ids", [])) if params else set()
        if not ids:
            return [{"src": src, "dst": dst} for src, dst in self.edges]
        rows = []
        for src, dst in self.edges:
            if src in ids or dst in ids:
                rows.append({"src": src, "dst": dst})
        return rows

def test_compute_ppr_scores_basic():
    # Simple graph: A -> B -> C
    edges = [("A", "B"), ("B", "C")]
    graph = MockGraph(edges)
    seeds = {"A": 1.0, "C": 0.2}

    ppr = compute_ppr_scores(graph, seeds, alpha=0.15, n_iter=10)

    assert "A" in ppr
    assert "B" in ppr
    assert "C" in ppr
    # B receives score propagation from A
    assert ppr["B"] > 0.0
    # PPR score for B should be elevated due to propagation from seed A
    assert ppr["A"] >= ppr["B"]

def test_compute_ppr_scores_empty():
    assert compute_ppr_scores(None, {}) == {}
    graph = MockGraph([])
    assert compute_ppr_scores(graph, {}) == {}

def test_compute_prone_embeddings():
    from src.mcp_server.graph_algorithms import compute_prone_embeddings
    edges = [("A", "B"), ("B", "C"), ("C", "A")]
    graph = MockGraph(edges)
    embs = compute_prone_embeddings(graph, dim=8)
    assert len(embs) == 3
    assert "A" in embs
    assert len(embs["A"]) == 8

def test_ltr_feature_extraction_and_scoring():
    from src.mcp_server.ltr_ranker import extract_candidate_features, compute_ltr_score

    candidate = {"id": "func_a", "name": "func_a"}
    feats = extract_candidate_features(
        candidate=candidate,
        query_vector_sim=0.85,
        fts_bm25_score=0.90,
        ppr_score=0.75,
        prone_sim=0.60,
        pagerank=0.50,
        in_degree=10
    )

    assert feats["vector_sim"] == 0.85
    assert feats["fts_score"] == 0.90
    assert feats["ppr_score"] == 0.75
    assert feats["prone_sim"] == 0.60
    assert 0.0 <= feats["in_degree"] <= 1.0

    score = compute_ltr_score(feats)
    assert score > 0.0


