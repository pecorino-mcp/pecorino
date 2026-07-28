import logging
import os
import threading
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger(__name__)

_ce_lock = threading.Lock()
_ce_pipeline = None

class CrossEncoderPipeline:
    def __init__(self, model_id=None):
        from src.mcp_server.config import settings
        self.model_id = model_id or settings.cross_encoder_model_repo
        self.session = None
        self.tokenizer = None
        self.is_ready = False
        self._initialize()

    def _initialize(self):
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as e:
            logger.error(f"Missing required dependencies for cross-encoder: {e}")
            return

        try:
            logger.info(f"Downloading/loading ONNX cross-encoder model: {self.model_id}")

            # Usually cross encoders on HF have an onnx/model.onnx file.
            # Let's check for standard layout. ms-marco-MiniLM-L-12-v2 might not have an 'onnx' subfolder natively
            # if they don't provide onnx natively. Wait, does cross-encoder/ms-marco-MiniLM-L-12-v2 have an onnx export?
            # Actually, `Xenova/ms-marco-MiniLM-L-12-v2` is typically what we want for ONNX models on HF.
            # Let's adjust the model ID if it's the standard HF one, we might need to rely on Xenova for ready-to-use ONNX.

            hf_token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")

            # Check if local path
            if os.path.isdir(self.model_id):
                model_path = os.path.join(self.model_id, "onnx", "model.onnx")
                tokenizer_path = os.path.join(self.model_id, "tokenizer.json")
            else:
                try:
                    # Assuming the model has an 'onnx/model.onnx' just like Xenova exports
                    model_path = hf_hub_download(repo_id=self.model_id, filename="onnx/model.onnx", token=hf_token, local_files_only=True)
                    tokenizer_path = hf_hub_download(repo_id=self.model_id, filename="tokenizer.json", token=hf_token, local_files_only=True)
                except Exception:
                    model_path = hf_hub_download(repo_id=self.model_id, filename="onnx/model.onnx", token=hf_token)
                    tokenizer_path = hf_hub_download(repo_id=self.model_id, filename="tokenizer.json", token=hf_token)

            self.tokenizer = Tokenizer.from_file(tokenizer_path)
            # Enable truncation to 512 tokens
            self.tokenizer.enable_truncation(max_length=512)

            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 2
            sess_options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(model_path, sess_options, providers=['CPUExecutionProvider'])
            self.is_ready = True
            logger.info("ONNX cross-encoder model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load ONNX cross-encoder model: {e}")
            self.is_ready = False

    def score_pairs(self, query: str, texts: List[str]) -> List[float]:
        if not self.is_ready or not self.session or not self.tokenizer:
            return [0.0] * len(texts)

        if not texts:
            return []

        # Prepare inputs as (query, text) pairs
        pairs = [(query, text) for text in texts]

        # Tokenize pairs. tokenizers supports encoding pairs: encode_batch([(q, t), ...])
        encoded = self.tokenizer.encode_batch(pairs)

        input_ids = [e.ids for e in encoded]
        attention_mask = [e.attention_mask for e in encoded]
        type_ids = [e.type_ids for e in encoded] # token_type_ids

        # Padding
        max_len = max(len(ids) for ids in input_ids)
        pad_id = self.tokenizer.token_to_id("[PAD]")
        if pad_id is None:
            pad_id = 0

        for j in range(len(input_ids)):
            pad_len = max_len - len(input_ids[j])
            if pad_len > 0:
                input_ids[j].extend([pad_id] * pad_len)
                attention_mask[j].extend([0] * pad_len)
                type_ids[j].extend([0] * pad_len)

        input_ids_arr = np.array(input_ids, dtype=np.int64)
        attention_mask_arr = np.array(attention_mask, dtype=np.int64)
        token_type_ids_arr = np.array(type_ids, dtype=np.int64)

        try:
            with _ce_lock:
                outputs = self.session.run(None, {
                    "input_ids": input_ids_arr,
                    "attention_mask": attention_mask_arr,
                    "token_type_ids": token_type_ids_arr
                })

            # Outputs shape is typically (batch_size, 1) for cross encoders
            logits = outputs[0]
            if len(logits.shape) > 1 and logits.shape[1] == 1:
                logits = logits.flatten()

            # Apply sigmoid if needed, but for ranking relative scores are fine.
            # However, sigmoid maps it nicely to [0,1].
            scores = 1.0 / (1.0 + np.exp(-logits))
            return [float(x) for x in scores]
        except Exception as e:
            logger.error(f"Cross-encoder inference failed: {e}")
            return [0.0] * len(texts)


def get_cross_encoder() -> CrossEncoderPipeline:
    global _ce_pipeline
    if _ce_pipeline is None:
        with _ce_lock:
            if _ce_pipeline is None:
                _ce_pipeline = CrossEncoderPipeline()
    return _ce_pipeline


def rerank(query: str, candidates: List[Dict[str, Any]], top_k: int = 10) -> List[Dict[str, Any]]:
    """Rerank a list of candidate dictionaries using the cross-encoder."""
    from src.mcp_server.config import settings
    if not settings.enable_cross_encoder:
        return candidates[:top_k]

    ce = get_cross_encoder()
    if not ce.is_ready:
        return candidates[:top_k]

    if not candidates:
        return []

    # Truncate candidates to score top N to save time
    limit_n = settings.cross_encoder_top_n
    to_score = candidates[:limit_n]
    the_rest = candidates[limit_n:]

    # Construct texts for scoring. Use name and body_text or summary
    texts = []
    for c in to_score:
        name = c.get("name", "")
        body = c.get("body_text", "") or ""
        summary = c.get("hcgs_summary", "") or ""
        # Provide a rich text representation
        texts.append(f"{name}\n{summary}\n{body}")

    scores = ce.score_pairs(query, texts)

    # Attach scores
    for i, c in enumerate(to_score):
        c["ce_score"] = round(float(scores[i]), 4)

    # Re-sort the scored candidates by ce_score descending
    to_score.sort(key=lambda x: x.get("ce_score", 0.0), reverse=True)

    # Combine back (the scored ones now float to the top)
    final_list = to_score + the_rest
    return final_list[:top_k]
