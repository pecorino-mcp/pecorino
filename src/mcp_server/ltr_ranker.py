import logging
import math
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

# Default weights derived from feature importance / heuristic model
DEFAULT_FEATURE_WEIGHTS = {
    "vector_sim": 0.40,
    "fts_score": 0.30,
    "ppr_score": 0.15,
    "prone_sim": 0.10,
    "in_degree": 0.05,
}

def extract_candidate_features(
    candidate: Dict[str, Any],
    query_vector_sim: float = 0.0,
    fts_bm25_score: float = 0.0,
    ppr_score: float = 0.0,
    prone_sim: float = 0.0,
    pagerank: float = 0.0,
    in_degree: int = 0
) -> Dict[str, float]:
    """
    Extract a normalized feature vector for a search result candidate.
    
    Feature Vector:
      [vector_sim, fts_score, ppr_score, prone_sim, pagerank, in_degree]
    """
    # Log-scale in_degree to bound feature value [0.0, 1.0]
    norm_in_degree = min(1.0, math.log1p(max(0, in_degree)) / 5.0)
    norm_pagerank = min(1.0, max(0.0, pagerank))

    return {
        "vector_sim": max(0.0, min(1.0, query_vector_sim)),
        "fts_score": max(0.0, min(1.0, fts_bm25_score)),
        "ppr_score": max(0.0, min(1.0, ppr_score)),
        "prone_sim": max(0.0, min(1.0, prone_sim)),
        "pagerank": norm_pagerank,
        "in_degree": norm_in_degree,
    }

def compute_ltr_score(
    features: Dict[str, float],
    weights: Dict[str, float] = None,
    model: Any = None
) -> float:
    """
    Compute unified Learning-to-Rank (LTR) score for a candidate.
    
    Uses XGBoost / Gradient Boosting model if available, otherwise falls back
    to weighted linear combination of normalized feature vectors.
    """
    if model is not None and hasattr(model, "predict"):
        try:
            import numpy as np
            feature_vector = np.array([[
                features.get("vector_sim", 0.0),
                features.get("fts_score", 0.0),
                features.get("ppr_score", 0.0),
                features.get("prone_sim", 0.0),
                features.get("pagerank", 0.0),
                features.get("in_degree", 0.0)
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
