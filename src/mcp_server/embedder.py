import hashlib
import logging
from typing import List

logger = logging.getLogger(__name__)

# Truncate texts to this many characters before embedding.
# all-MiniLM-L12-v2 has a 512 token window (~2000 chars).
# Sending longer texts wastes CPU cycles for zero benefit.
_MAX_EMBED_CHARS = 2048

# Process this many texts per model.encode() call.
# Larger batches amortise model overhead on CPU.
_BATCH_SIZE = 128


class Embedder:
    def __init__(self, db_conn):
        self.db_conn = db_conn
        self.model_name = "all-MiniLM-L12-v2"
        self._model = None
        self._ensure_cache_table()

    def _ensure_cache_table(self):
        self.db_conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings_cache (
                text_hash VARCHAR PRIMARY KEY,
                text VARCHAR,
                embedding DOUBLE[384],
                model VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def _load_model(self):
        if self._model is not None:
            return
        try:
            import os
            import torch
            max_threads = max(1, int((os.cpu_count() or 4) * 0.75))
            torch.set_num_threads(max_threads)
            os.environ["OMP_NUM_THREADS"] = str(max_threads)
            os.environ["TOKENIZERS_PARALLELISM"] = "false"

            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            self._encode = lambda texts: self._model.encode(
                texts, batch_size=_BATCH_SIZE, show_progress_bar=False
            ).tolist()
            logger.info(f"Loaded sentence-transformers model {self.model_name}")
        except (ImportError, Exception) as e:
            logger.warning(f"sentence-transformers/torch unavailable ({e}). Falling back to fastembed.")
            try:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L12-v2", threads=max_threads)
                self._encode = lambda texts: [list(v) for v in self._model.embed(texts)]
                logger.info(f"Loaded fastembed model {self.model_name}")
            except (ImportError, Exception) as e2:
                logger.warning(f"Neither sentence-transformers nor fastembed is available ({e2}). Disabling vector embeddings.")
                self._model = None
                self._encode = None

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        # Truncate long texts — the model window is only ~256 tokens
        truncated = [t[:_MAX_EMBED_CHARS] if len(t) > _MAX_EMBED_CHARS else t for t in texts]

        # Hash truncated texts for caching
        hashes = [hashlib.sha256(t.encode('utf-8')).hexdigest() for t in truncated]

        # Check cache
        hash_list_str = ", ".join(f"'{h}'" for h in hashes)
        cached = {}
        if hash_list_str:
            res = self.db_conn.execute(f"SELECT text_hash, embedding FROM embeddings_cache WHERE text_hash IN ({hash_list_str}) AND model = '{self.model_name}'").fetchall()
            for r in res:
                cached[r[0]] = r[1]

        # Embed missing
        missing_indices = [i for i, h in enumerate(hashes) if h not in cached]
        if missing_indices:
            self._load_model()
            if self._encode is None:
                return []
            missing_texts = [truncated[i] for i in missing_indices]
            logger.info(f"Embedding {len(missing_texts)} texts (batch_size={_BATCH_SIZE})...")
            new_embeddings = self._encode(missing_texts)

            # Cache new embeddings
            insert_data = []
            for i, idx in enumerate(missing_indices):
                h = hashes[idx]
                t = truncated[idx]
                emb = new_embeddings[i]
                cached[h] = emb
                insert_data.append((h, t, emb, self.model_name))

            if insert_data:
                # Fast insert via pandas DataFrame to avoid DuckDB executemany slowness with arrays
                import pandas as pd
                df = pd.DataFrame(insert_data, columns=["text_hash", "text", "embedding", "model"])
                self.db_conn.execute("INSERT OR REPLACE INTO embeddings_cache SELECT * FROM df")

        # Return in original order
        return [cached[h] for h in hashes]
