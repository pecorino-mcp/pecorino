"""Tests for Phase 4: LTR Integration."""

from src.mcp_server.ltr_ranker import (
    DEFAULT_FEATURE_WEIGHTS,
    compute_ltr_score,
    extract_candidate_features,
)


class TestFeatureWeights:
    """Verify structural invariants on DEFAULT_FEATURE_WEIGHTS."""

    def test_weights_sum_to_one(self):
        """All weights should sum to ~1.0."""
        total = sum(DEFAULT_FEATURE_WEIGHTS.values())
        assert 0.99 <= total <= 1.01, f"Weights sum to {total}"

    def test_every_extracted_feature_has_weight(self):
        """Every feature returned by extract_candidate_features must have a non-zero
        weight in DEFAULT_FEATURE_WEIGHTS, and vice-versa."""
        feats = extract_candidate_features({})
        feat_keys = set(feats.keys())
        weight_keys = set(DEFAULT_FEATURE_WEIGHTS.keys())
        assert feat_keys == weight_keys, (
            f"Mismatched keys — extracted but no weight: {feat_keys - weight_keys}, "
            f"weight but not extracted: {weight_keys - feat_keys}"
        )


class TestExtractCandidateFeatures:
    """Unit tests for feature extraction and normalization."""

    def test_all_features_in_unit_range(self):
        """All features should be clamped / normalized to [0, 1]."""
        candidate = {
            "pagerank": 0.85,
            "in_degree": 15,
            "out_degree": 3,
            "complexity": 10,
            "instability": 0.25,
            "coupling": 18.0,
            "depth": 2,
            "inheritance_depth": 1,
            "betweenness": 0.12,
            "git_commit_count": 45,
            "git_days_since_change": 10,
            "git_churn": 500,
            "git_authors": 4,
            "git_bug_fix_ratio": 0.35,
            "git_survival_days": 180,
            "git_ownership_entropy": 1.2,
        }
        feats = extract_candidate_features(
            candidate, fts_bm25_score=4.5, query_vector_sim=0.88
        )
        for key, val in feats.items():
            assert 0.0 <= val <= 1.0, f"Feature {key}={val} out of [0, 1]"

    def test_higher_commits_yields_higher_feature(self):
        """A candidate with more git commits should score higher on git_commit_count."""
        feats_low = extract_candidate_features({"git_commit_count": 2})
        feats_high = extract_candidate_features({"git_commit_count": 200})
        assert feats_high["git_commit_count"] > feats_low["git_commit_count"]

    def test_recency_decays_smoothly(self):
        """Exponential decay should give distinct values at 30, 365, and 730 days."""
        feats_30 = extract_candidate_features({"git_days_since_change": 30})
        feats_365 = extract_candidate_features({"git_days_since_change": 365})
        feats_730 = extract_candidate_features({"git_days_since_change": 730})

        assert feats_30["git_days_since_change"] > feats_365["git_days_since_change"]
        assert feats_365["git_days_since_change"] > feats_730["git_days_since_change"]
        # 730-day-old should still produce a non-zero score (unlike linear clamp)
        assert feats_730["git_days_since_change"] > 0.0

    def test_pagerank_log_scaling_spreads_tiny_values(self):
        """PageRank 0.001 should not normalize to near-zero; log-scaling should help."""
        feats = extract_candidate_features({"pagerank": 0.001})
        # With log1p(0.001 * 1000) / log1p(1000) ≈ log1p(1) / log1p(1000) ≈ 0.1
        assert feats["pagerank"] > 0.05, (
            f"PageRank 0.001 normalized to {feats['pagerank']}, expected > 0.05"
        )

    def test_betweenness_log_scaling_spreads_tiny_values(self):
        """Betweenness 0.005 should not normalize to near-zero."""
        feats = extract_candidate_features({"betweenness": 0.005})
        # log1p(0.005 * 10000) / log1p(10000) ≈ log1p(50) / log1p(10000) ≈ 0.43
        assert feats["betweenness"] > 0.3, (
            f"Betweenness 0.005 normalized to {feats['betweenness']}, expected > 0.3"
        )

    def test_zero_candidate_produces_baseline(self):
        """An empty candidate with no signals should produce all-zero features
        (except git_days_since_change which decays from 0 → 1.0)."""
        feats = extract_candidate_features({})
        for key, val in feats.items():
            if key == "git_days_since_change":
                # exp(-0/260) = 1.0 — freshest possible
                assert abs(val - 1.0) < 1e-9
            else:
                assert val == 0.0, f"Feature {key}={val} should be 0.0 for empty candidate"


class TestComputeLTRScore:
    """Unit tests for the LTR score computation."""

    def test_score_is_positive_for_strong_candidate(self):
        """A candidate with strong FTS + vector should produce a meaningfully positive score."""
        feats = extract_candidate_features(
            {"pagerank": 0.5, "in_degree": 5, "git_commit_count": 20},
            fts_bm25_score=2.0,
            query_vector_sim=0.7,
        )
        score = compute_ltr_score(feats)
        assert score > 0.0
        assert isinstance(score, float)

    def test_strong_candidate_beats_weak(self):
        """A candidate with high FTS + vector should outscore one with neither."""
        strong = extract_candidate_features(
            {"pagerank": 0.5, "in_degree": 10, "git_commit_count": 50},
            fts_bm25_score=8.0,
            query_vector_sim=0.95,
        )
        weak = extract_candidate_features(
            {"pagerank": 0.001, "in_degree": 0, "git_commit_count": 1},
            fts_bm25_score=0.1,
            query_vector_sim=0.05,
        )
        assert compute_ltr_score(strong) > compute_ltr_score(weak)

    def test_custom_weights_override_defaults(self):
        """Custom weights should change the score relative to defaults."""
        feats = extract_candidate_features(
            {"git_commit_count": 100},
            fts_bm25_score=1.0,
            query_vector_sim=0.5,
        )
        default_score = compute_ltr_score(feats)

        # Boost git_commit_count weight dramatically
        custom_weights = {k: 0.0 for k in DEFAULT_FEATURE_WEIGHTS}
        custom_weights["git_commit_count"] = 1.0
        custom_score = compute_ltr_score(feats, weights=custom_weights)

        assert custom_score != default_score
        assert custom_score == feats["git_commit_count"]  # purely that one feature

    def test_empty_features_score_zero(self):
        """A candidate with all-zero features (except recency) should score near
        the recency-weight contribution only."""
        feats = extract_candidate_features({})
        score = compute_ltr_score(feats)
        # Only git_days_since_change is 1.0, everything else is 0.0
        expected = DEFAULT_FEATURE_WEIGHTS["git_days_since_change"] * 1.0
        assert abs(score - expected) < 1e-9
