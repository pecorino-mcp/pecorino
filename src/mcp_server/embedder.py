import hashlib
import json
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

class Embedder:
    def __init__(self, db_conn):
        self.db_conn = db_conn
        self.model_name = "all-MiniLM-L6-v2"
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
            self._encode = lambda texts: self._model.encode(texts).tolist()
            logger.info(f"Loaded sentence-transformers model {self.model_name}")
        except ImportError:
            logger.warning("sentence-transformers not found. Falling back to fastembed.")
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2", threads=max_threads)
            self._encode = lambda texts: [list(v) for v in self._model.embed(texts)]
            logger.info(f"Loaded fastembed model {self.model_name}")

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        # Hash texts
        hashes = [hashlib.sha256(t.encode('utf-8')).hexdigest() for t in texts]
        
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
            missing_texts = [texts[i] for i in missing_indices]
            logger.info(f"Embedding {len(missing_texts)} missing texts...")
            new_embeddings = self._encode(missing_texts)
            
            # Cache new embeddings
            insert_data = []
            for i, idx in enumerate(missing_indices):
                h = hashes[idx]
                t = texts[idx]
                emb = new_embeddings[i]
                cached[h] = emb
                insert_data.append((h, t, emb, self.model_name))
            
            if insert_data:
                # Fast insert via executemany
                self.db_conn.executemany(
                    "INSERT OR REPLACE INTO embeddings_cache (text_hash, text, embedding, model) VALUES (?, ?, ?, ?)",
                    insert_data
                )
                
        # Return in original order
        return [cached[h] for h in hashes]
