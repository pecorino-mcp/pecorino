import collections
import threading

ALLOWED_API_TYPES = frozenset({"index", "graph"})

_API_CACHE_MAX_SIZE = 10
_API_CACHE = collections.OrderedDict()
_API_CACHE_LOCK = threading.Lock()

def _get_cached_api(repo_root: str, db_path: str, api_type: str):
    if api_type not in ALLOWED_API_TYPES:
        raise ValueError(f"Invalid api_type: {api_type}")
    key = (db_path, api_type)
    with _API_CACHE_LOCK:
        if key in _API_CACHE:
            _API_CACHE.move_to_end(key)
            return _API_CACHE[key]
        
    if api_type == "index":
        from src.mcp_server.index_db import CodeSearchIndex
        new_api = CodeSearchIndex(db_path=db_path, read_only=True)
    elif api_type == "graph":
        from src.mcp_server.graph_api import GraphAPI
        new_api = GraphAPI(repo_path=repo_root)
        
    with _API_CACHE_LOCK:
        if key in _API_CACHE:
            if hasattr(new_api, 'close'):
                new_api.close()
            return _API_CACHE[key]
        _API_CACHE[key] = new_api
        
        if len(_API_CACHE) > _API_CACHE_MAX_SIZE:
            oldest_key, oldest_api = _API_CACHE.popitem(last=False)
            if hasattr(oldest_api, 'close'):
                try:
                    oldest_api.close()
                except Exception:
                    pass
                
    return new_api

def clear_api_cache():
    with _API_CACHE_LOCK:
        while _API_CACHE:
            _, api = _API_CACHE.popitem()
            if hasattr(api, 'close'):
                try:
                    api.close()
                except Exception:
                    pass

def clear_index_cache():
    """Clear only CodeSearchIndex (DuckDB) cache entries, preserving GraphAPI.

    This avoids destroying the GraphAPI's PageRank cache on every auto-sync,
    which would force expensive recomputation on the next pagerank view.
    """
    with _API_CACHE_LOCK:
        keys_to_remove = [k for k in _API_CACHE if k[1] == "index"]
        for k in keys_to_remove:
            api = _API_CACHE.pop(k)
            if hasattr(api, 'close'):
                try:
                    api.close()
                except Exception:
                    pass
        # Invalidate pagerank cache on any remaining GraphAPI instances
        for k, api in _API_CACHE.items():
            if k[1] == "graph" and hasattr(api, 'invalidate_pagerank_cache'):
                api.invalidate_pagerank_cache()
