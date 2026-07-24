import logging
import math
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Default weights derived from feature importance / heuristic model.
# All extracted features must have a corresponding weight; sum ≈ 1.0.
DEFAULT_FEATURE_WEIGHTS: Dict[str, float] = {
    "fts_score": 0.26,
    "vector_sim": 0.22,
    "ppr_score": 0.06,
    "prone_sim": 0.02,
    "pagerank": 0.08,
    "in_degree": 0.04,
    "out_degree": 0.02,
    "betweenness": 0.04,
    "git_commit_count": 0.04,
    "git_days_since_change": 0.04,
    "git_churn": 0.02,
    "git_ownership_entropy": 0.02,
    "git_bug_fix_ratio": 0.02,
    "git_authors": 0.01,
    "git_survival_days": 0.01,
    "instability": 0.02,
    "coupling": 0.02,
    "complexity": 0.03,
    "depth": 0.02,
    "inheritance_depth": 0.01,
}

# Sanity-check at import time: weights should sum to ~1.0
_weight_sum = sum(DEFAULT_FEATURE_WEIGHTS.values())
assert 0.99 <= _weight_sum <= 1.01, f"Feature weights sum to {_weight_sum}, expected ~1.0"


def extract_candidate_features(
    candidate: Dict[str, Any],
    query_vector_sim: float = 0.0,
    fts_bm25_score: float = 0.0,
    ppr_score: float = 0.0,
    prone_sim: float = 0.0,
    pagerank: float = 0.0,
    in_degree: int = 0,
) -> Dict[str, float]:
    """Extract a normalized feature vector for a search result candidate.

    All features are normalized to [0.0, 1.0] using appropriate transforms:
    - Raw probabilities / ratios: simple clamp
    - Counts / integers: log1p scaling with empirical caps
    - Time features: exponential decay
    - Graph centrality (pagerank, betweenness): log1p scaling to spread
      the typical tiny values across a wider [0, 1] range
    """
    pagerank_val = float(candidate.get("pagerank", pagerank) or pagerank)
    in_degree_val = int(candidate.get("in_degree", in_degree) or in_degree)
    out_degree_val = int(candidate.get("out_degree", 0) or 0)
    complexity_val = int(candidate.get("complexity", 0) or 0)
    instability_val = float(candidate.get("instability", 0.0) or 0.0)
    coupling_val = float(candidate.get("coupling", 0.0) or 0.0)
    depth_val = int(candidate.get("depth", 0) or 0)
    inheritance_depth_val = int(candidate.get("inheritance_depth", 0) or 0)
    betweenness_val = float(candidate.get("betweenness", 0.0) or 0.0)

    git_commit_count = int(candidate.get("git_commit_count", 0) or 0)
    git_days_since_change = int(candidate.get("git_days_since_change", 0) or 0)
    git_churn = int(candidate.get("git_churn", 0) or 0)
    git_authors = int(candidate.get("git_authors", 0) or 0)
    git_bug_fix_ratio = float(candidate.get("git_bug_fix_ratio", 0.0) or 0.0)
    git_survival_days = int(candidate.get("git_survival_days", 0) or 0)
    git_ownership_entropy = float(candidate.get("git_ownership_entropy", 0.0) or 0.0)

    # --- FTS normalization ---
    # BM25 scores from Tantivy are typically < 30; we cap & scale to [0, 1].
    if fts_bm25_score <= 1.0:
        norm_fts = max(0.0, min(1.0, fts_bm25_score))
    else:
        norm_fts = max(0.0, min(1.0, fts_bm25_score / 10.0))

    # --- Recency: exponential decay (half-life ≈ 180 days) ---
    # Provides smooth degradation: 90-day-old → ~0.68, 365-day-old → ~0.13,
    # 730-day-old → ~0.018 — much better than the linear clamp-at-365 approach.
    norm_recency = math.exp(-git_days_since_change / 260.0)

    # --- PageRank: log1p scaling ---
    # Typical PageRank values in a 1k-node graph are 0.0001–0.01.
    # log1p(pr * 1000) / log1p(1000) spreads these across [0, 1].
    norm_pagerank = min(1.0, math.log1p(pagerank_val * 1000.0) / math.log1p(1000.0))

    # --- Betweenness: log1p scaling ---
    # Raw betweenness in [0, 1] but typically << 0.01 for large graphs.
    # log1p(b * 10000) / log1p(10000) stretches the range.
    norm_betweenness = min(1.0, math.log1p(betweenness_val * 10000.0) / math.log1p(10000.0))

    return {
        "fts_score": norm_fts,
        "vector_sim": max(0.0, min(1.0, query_vector_sim)),
        "ppr_score": max(0.0, min(1.0, ppr_score)),
        "prone_sim": max(0.0, min(1.0, prone_sim)),
        "pagerank": norm_pagerank,
        "in_degree": min(1.0, math.log1p(max(0, in_degree_val)) / 5.0),
        "out_degree": min(1.0, math.log1p(max(0, out_degree_val)) / 5.0),
        "complexity": min(1.0, math.log1p(max(0, complexity_val)) / 4.0),
        "instability": max(0.0, min(1.0, instability_val)),
        "coupling": min(1.0, math.log1p(max(0.0, coupling_val)) / 5.0),
        "depth": min(1.0, max(0, depth_val) / 10.0),
        "inheritance_depth": min(1.0, max(0, inheritance_depth_val) / 5.0),
        "betweenness": norm_betweenness,
        "git_commit_count": min(1.0, math.log1p(max(0, git_commit_count)) / 6.0),
        "git_days_since_change": norm_recency,
        "git_churn": min(1.0, math.log1p(max(0, git_churn)) / 10.0),
        "git_authors": min(1.0, math.log1p(max(0, git_authors)) / 3.0),
        "git_bug_fix_ratio": max(0.0, min(1.0, git_bug_fix_ratio)),
        "git_survival_days": min(1.0, max(0, git_survival_days) / 730.0),
        "git_ownership_entropy": min(1.0, max(0.0, git_ownership_entropy) / 3.0),
    }


def compute_ltr_score(
    features: Dict[str, float],
    weights: Dict[str, float] | None = None,
    model: Any = None,
) -> float:
    """Compute unified Learning-to-Rank (LTR) score for a candidate.

    Uses XGBoost / Gradient Boosting model if available, otherwise falls back
    to weighted linear combination of normalized feature vectors.
    """
    if model is not None and hasattr(model, "predict"):
        try:
            import numpy as np

            feature_vector = np.array([[
                features.get(k, 0.0) for k in DEFAULT_FEATURE_WEIGHTS.keys()
            ]])
            preds = model.predict(feature_vector)
            return float(preds[0])
        except Exception as e:
            logger.debug(f"Model prediction failed, falling back to weighted sum: {e}")

    # Fallback to weighted linear combination
    w = weights if weights is not None else DEFAULT_FEATURE_WEIGHTS
    score = 0.0
    for feat_name, feat_val in features.items():
        weight = w.get(feat_name, 0.0)
        score += weight * feat_val

    return score
