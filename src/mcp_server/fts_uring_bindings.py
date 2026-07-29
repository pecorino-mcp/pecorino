import ctypes
import os
import hashlib
from typing import List, Tuple
from pathlib import Path

# Constants matching C header
MAX_FTS_FIELDS = 4
EMBEDDING_DIM = 384

# Ctypes structures
class WALManager(ctypes.Structure):
    _fields_ = [
        ("wal_fd", ctypes.c_int),
        ("filepath", ctypes.c_char * 256),
        ("current_lsn", ctypes.c_uint64)
    ]

class BufferPoolManager(ctypes.Structure):
    _fields_ = [
        ("data_fd", ctypes.c_int),
        ("ring", ctypes.c_void_p), # io_uring struct is large, opaque pointer is fine if we don't access it from Python
        ("padding", ctypes.c_byte * 1024) # Pad it to ensure enough space. Actually, we shouldn't pass by value.
    ]

# We will just use opaque pointers for the engine and shards to avoid redefining huge C structs in Python
class UnifiedEngine(ctypes.Structure):
    pass

class UnifiedShard(ctypes.Structure):
    pass

class FTSUringEngine:
    def __init__(self, so_path: str):
        self.lib = ctypes.CDLL(so_path)
        
        # python_insert_document(..., const char *text)
        self.lib.python_insert_document.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_char_p
        ]
        self.lib.python_insert_document.restype = ctypes.c_int
            
    def insert_document(self, node_uuid_bytes: bytes, tf: List[int], dl: List[int], embedding: List[float], text: str = ""):
        if len(node_uuid_bytes) != 16:
            raise ValueError("node_uuid_bytes must be 16 bytes")
            
        tf_arr = (ctypes.c_uint32 * MAX_FTS_FIELDS)(*tf[:MAX_FTS_FIELDS])
        dl_arr = (ctypes.c_uint32 * MAX_FTS_FIELDS)(*dl[:MAX_FTS_FIELDS])
        
        if embedding:
            emb_arr = (ctypes.c_float * EMBEDDING_DIM)(*embedding[:EMBEDDING_DIM])
        else:
            emb_arr = (ctypes.c_float * EMBEDDING_DIM)(*[0.0]*EMBEDDING_DIM)
            
        uuid_arr = (ctypes.c_uint8 * 16)(*node_uuid_bytes)
        text_bytes = text.encode('utf-8') if text else None
        return self.lib.python_insert_document(uuid_arr, tf_arr, dl_arr, emb_arr, text_bytes)
        
    def close(self):
        pass
