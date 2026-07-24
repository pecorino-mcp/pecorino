"""Tests for Phase 5: Cross-Encoder Integration."""

import pytest

from src.mcp_server.config import settings
from src.mcp_server.cross_encoder import rerank

# Note: We don't want to actually download a 130MB model during unit tests if it's not cached.
# But if it is cached or we mock it, we should test the logic.
# For these unit tests, we'll mock the session to return dummy scores.

class DummySession:
    def run(self, output_names, input_feed):
        input_ids = input_feed["input_ids"]
        batch_size = input_ids.shape[0]
        # Return random logits for testing
        import numpy as np
        return [np.random.randn(batch_size, 1).astype(np.float32)]

class DummyTokenizer:
    def enable_truncation(self, max_length):
        pass

    def encode_batch(self, pairs):
        class Encoded:
            def __init__(self, ids, mask, type_ids):
                self.ids = ids
                self.attention_mask = mask
                self.type_ids = type_ids

        return [Encoded([1, 2, 3], [1, 1, 1], [0, 0, 0]) for _ in pairs]

    def token_to_id(self, token):
        return 0

@pytest.fixture
def mock_ce_pipeline(monkeypatch):
    """Mocks the CrossEncoderPipeline to avoid actual model loading."""
    import src.mcp_server.cross_encoder as ce_module

    # We patch the `_initialize` method so it sets up dummy session/tokenizer
    original_init = ce_module.CrossEncoderPipeline._initialize

    def mock_initialize(self):
        self.session = DummySession()
        self.tokenizer = DummyTokenizer()
        self.is_ready = True

    monkeypatch.setattr(ce_module.CrossEncoderPipeline, "_initialize", mock_initialize)

    # Force re-initialization of singleton
    ce_module._ce_pipeline = None
    pipeline = ce_module.get_cross_encoder()

    yield pipeline

    # Cleanup
    ce_module._ce_pipeline = None
    monkeypatch.setattr(ce_module.CrossEncoderPipeline, "_initialize", original_init)

class TestCrossEncoder:
    """Unit tests for Phase 5 Cross-Encoder."""

    def test_rerank_with_disabled_setting(self, monkeypatch, mock_ce_pipeline):
        """If enable_cross_encoder is False, rerank should be a passthrough."""
        monkeypatch.setattr(settings, "enable_cross_encoder", False)

        candidates = [
            {"id": "1", "name": "foo", "score": 10},
            {"id": "2", "name": "bar", "score": 9},
        ]

        result = rerank("query", candidates, top_k=10)
        assert len(result) == 2
        assert "ce_score" not in result[0]

    def test_rerank_adds_ce_scores(self, monkeypatch, mock_ce_pipeline):
        """If enabled, rerank should add ce_score and sort by it."""
        monkeypatch.setattr(settings, "enable_cross_encoder", True)
        monkeypatch.setattr(settings, "cross_encoder_top_n", 30)

        candidates = [
            {"id": "1", "name": "foo", "score": 10},
            {"id": "2", "name": "bar", "score": 9},
            {"id": "3", "name": "baz", "score": 8},
        ]

        # We'll mock the score_pairs method to return specific scores
        def mock_score(query, texts):
            return [0.1, 0.9, 0.5]  # so "bar" should win (index 1)

        monkeypatch.setattr(mock_ce_pipeline, "score_pairs", mock_score)

        result = rerank("query", candidates, top_k=10)

        assert len(result) == 3
        assert result[0]["id"] == "2"
        assert result[0]["ce_score"] == 0.9
        assert result[1]["id"] == "3"
        assert result[1]["ce_score"] == 0.5
        assert result[2]["id"] == "1"
        assert result[2]["ce_score"] == 0.1

    def test_rerank_truncates_to_top_k(self, monkeypatch, mock_ce_pipeline):
        """Rerank should respect the top_k argument."""
        monkeypatch.setattr(settings, "enable_cross_encoder", True)

        candidates = [{"id": str(i), "name": f"f_{i}"} for i in range(20)]
        result = rerank("query", candidates, top_k=5)

        assert len(result) == 5

    def test_rerank_scores_only_top_n(self, monkeypatch, mock_ce_pipeline):
        """Rerank should only compute scores for settings.cross_encoder_top_n candidates."""
        monkeypatch.setattr(settings, "enable_cross_encoder", True)
        monkeypatch.setattr(settings, "cross_encoder_top_n", 5)

        candidates = [{"id": str(i), "name": f"f_{i}"} for i in range(15)]

        # Mock score pairs to verify how many it scores
        called_with_texts = []
        def mock_score(query, texts):
            called_with_texts.extend(texts)
            return [0.5] * len(texts)

        monkeypatch.setattr(mock_ce_pipeline, "score_pairs", mock_score)

        result = rerank("query", candidates, top_k=10)

        assert len(called_with_texts) == 5
        assert len(result) == 10
        # The first 5 should have ce_score, the rest shouldn't
        assert "ce_score" in result[0]
        assert "ce_score" not in result[6]

    def test_passthrough_when_model_fails(self, monkeypatch, mock_ce_pipeline):
        """If model throws exception or isn't ready, should safely passthrough."""
        monkeypatch.setattr(settings, "enable_cross_encoder", True)

        mock_ce_pipeline.is_ready = False

        candidates = [{"id": "1", "name": "foo"}]
        result = rerank("query", candidates, top_k=10)
        assert len(result) == 1
        assert "ce_score" not in result[0]
