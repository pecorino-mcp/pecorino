import ctypes
from typing import List

# Constants matching C header
MAX_FTS_FIELDS = 4
EMBEDDING_DIM = 384


class FTSUringEngine:
    def __init__(self, so_path: str):
        self.lib = ctypes.CDLL(so_path)

        # python_insert_document(..., const char *text, uint64_t mtime, const char* repo, const char* filepath, const char* kind, const char* lang)
        self.lib.python_insert_document.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p
        ]
        self.lib.python_insert_document.restype = ctypes.c_int

    def insert_document(self, node_uuid_bytes: bytes, tf: List[int], dl: List[int], embedding: List[float], text: str = "", mtime: int = 0, repo: str = "", filepath: str = "", kind: str = "", lang: str = ""):
        if len(node_uuid_bytes) != 16:
            raise ValueError("node_uuid_bytes must be 16 bytes")

        tf_padded = (list(tf) + [0] * MAX_FTS_FIELDS)[:MAX_FTS_FIELDS]
        dl_padded = (list(dl) + [0] * MAX_FTS_FIELDS)[:MAX_FTS_FIELDS]
        tf_arr = (ctypes.c_uint32 * MAX_FTS_FIELDS)(*tf_padded)
        dl_arr = (ctypes.c_uint32 * MAX_FTS_FIELDS)(*dl_padded)

        if embedding:
            emb_padded = (list(embedding) + [0.0] * EMBEDDING_DIM)[:EMBEDDING_DIM]
            emb_arr = (ctypes.c_float * EMBEDDING_DIM)(*emb_padded)
        else:
            emb_arr = (ctypes.c_float * EMBEDDING_DIM)(*([0.0] * EMBEDDING_DIM))

        uuid_arr = (ctypes.c_uint8 * 16)(*node_uuid_bytes)
        text_bytes = text.encode('utf-8') if text else None
        repo_bytes = repo.encode('utf-8') if repo else None
        filepath_bytes = filepath.encode('utf-8') if filepath else None
        kind_bytes = kind.encode('utf-8') if kind else None
        lang_bytes = lang.encode('utf-8') if lang else None
        return self.lib.python_insert_document(uuid_arr, tf_arr, dl_arr, emb_arr, text_bytes, ctypes.c_uint64(mtime), repo_bytes, filepath_bytes, kind_bytes, lang_bytes)

    def close(self):
        pass
