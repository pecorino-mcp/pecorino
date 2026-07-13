import os
import logging
import numpy as np

logger = logging.getLogger(__name__)

import threading

_onnx_lock = threading.Lock()

class EmbeddingPipeline:
    def __init__(self, model_id=None):
        from src.mcp_server.config import settings
        self.model_id = model_id or settings.embedding_model
        self.embedding_dim = settings.embedding_dim
        self.session = None
        self.tokenizer = None
        self._initialize()

    def _initialize(self):
        try:
            from huggingface_hub import hf_hub_download
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            logger.error(f"Missing required dependencies for embeddings: {e}")
            return

        try:
            # We must set matmul_nbits to avoid parsing issues if the ONNX model is quantized
            # Nomic v1.5 text embedding model. The onnx version is usually in the "onnx" subfolder
            logger.info("Downloading/loading ONNX embedding model...")
            model_path = hf_hub_download(repo_id=self.model_id, filename="onnx/model.onnx")
            tokenizer_path = hf_hub_download(repo_id=self.model_id, filename="tokenizer.json")
            
            self.tokenizer = Tokenizer.from_file(tokenizer_path)
            # Enable truncation to avoid massive memory spikes
            self.tokenizer.enable_truncation(max_length=512)
            
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 2
            sess_options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(model_path, sess_options, providers=['CPUExecutionProvider'])
            logger.info("ONNX embedding model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load ONNX model: {e}")

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        if not self.session or not self.tokenizer:
            return [[0.0] * self.embedding_dim for _ in queries]
        if not queries:
            return []
        
        if "nomic" in self.model_id.lower():
            prefixed = [f"search_query: {q}" for q in queries]
        else:
            prefixed = queries
            
        return self.embed_batch(prefixed)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.session or not self.tokenizer:
            logger.warning("Embedding session not initialized, returning zero vectors.")
            return [[0.0] * self.embedding_dim for _ in texts]
        
        if not texts:
            return []

        # Nomic v1.5 uses prefixes
        if "nomic" in self.model_id.lower():
            prefixed_texts = [f"search_document: {t}" for t in texts]
        else:
            prefixed_texts = texts
        
        all_embeddings = []
        batch_size = 16
        
        for i in range(0, len(prefixed_texts), batch_size):
            chunk = prefixed_texts[i:i+batch_size]
            
            # Tokenize
            encoded = self.tokenizer.encode_batch(chunk)
            input_ids = [e.ids for e in encoded]
            attention_mask = [e.attention_mask for e in encoded]
            
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
                
            input_ids_arr = np.array(input_ids, dtype=np.int64)
            attention_mask_arr = np.array(attention_mask, dtype=np.int64)
            token_type_ids_arr = np.zeros_like(input_ids_arr, dtype=np.int64)
            
            # Run ONNX inference under lock to prevent multi-thread RAM blowup
            with _onnx_lock:
                outputs = self.session.run(None, {
                    "input_ids": input_ids_arr,
                    "attention_mask": attention_mask_arr,
                    "token_type_ids": token_type_ids_arr
                })
            
            embeddings = outputs[0]
            
            # Mean pooling
            mask_expanded = np.expand_dims(attention_mask_arr, -1)
            sum_embeddings = np.sum(embeddings * mask_expanded, axis=1)
            sum_mask = np.clip(np.sum(mask_expanded, axis=1), a_min=1e-9, a_max=None)
            pooled = sum_embeddings / sum_mask
            
            # Normalize to L2 norm = 1.0
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            norm = np.clip(norm, a_min=1e-9, a_max=None)
            pooled = pooled / norm
            
            all_embeddings.extend(pooled.tolist())
            
        return all_embeddings
