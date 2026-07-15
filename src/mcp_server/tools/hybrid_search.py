import logging
from typing import Optional, Dict, Any, List
from src.mcp_server.index_db import CodeSearchIndex, get_db_path_for_repo
from src.mcp_server.embedder import Embedder

logger = logging.getLogger(__name__)

async def do_semantic_search(
    query: str,
    target: str,
    limit: int = 10,
    allow_external: bool = True,
    ctx: Optional[Any] = None
) -> Dict[str, Any]:
    db_path = get_db_path_for_repo(target)
    idx = CodeSearchIndex(db_path=db_path)
    graph = idx._ensure_graph()
    embedder = Embedder(idx._conn)
    
    # Embed the query
    q_emb = embedder.embed_texts([query])[0]
    
    # Kuzu query using vector index
    cypher = f"""
    MATCH (n:CodeNode)
    WHERE n.embedding IS NOT NULL
    WITH n, ARRAY_COSINE_SIMILARITY(n.embedding, {q_emb}) AS sim
    ORDER BY sim DESC
    LIMIT {limit}
    RETURN n.id, n.kind, n.name, n.file, sim
    """
    
    res = graph.query(cypher)
    return {"results": res, "query": query, "type": "semantic_search"}

async def do_hybrid_search(
    query: str,
    target: str,
    limit: int = 10,
    allow_external: bool = True,
    ctx: Optional[Any] = None
) -> Dict[str, Any]:
    db_path = get_db_path_for_repo(target)
    idx = CodeSearchIndex(db_path=db_path)
    graph = idx._ensure_graph()
    embedder = Embedder(idx.conn)
    
    q_emb = embedder.embed_texts([query])[0]
    
    # Vector results
    vec_query = f"""
    MATCH (n:CodeNode)
    WHERE n.embedding IS NOT NULL
    WITH n, ARRAY_COSINE_SIMILARITY(n.embedding, {q_emb}) AS vec_score
    ORDER BY vec_score DESC LIMIT 50
    RETURN n.id, n.kind, n.name, n.file, vec_score
    """
    vec_res = graph.query(vec_query)
    
    scores = {}
    for r in vec_res:
        nid = r['n.id']
        vec_score = r['vec_score']
        scores[nid] = {'node': r, 'vec': vec_score, 'fts': 0, 'total': 0.4 * vec_score}
        
    sorted_res = sorted(scores.values(), key=lambda x: x['total'], reverse=True)[:limit]
    
    return {"results": [s['node'] for s in sorted_res], "query": query, "type": "hybrid_search"}

async def do_explain(
    node_id: str,
    target: str,
    allow_external: bool = True,
    ctx: Optional[Any] = None
) -> Dict[str, Any]:
    db_path = get_db_path_for_repo(target)
    idx = CodeSearchIndex(db_path=db_path)
    graph = idx._ensure_graph()
    
    cypher = f"MATCH (n)-[e]->(m) WHERE n.id = '{node_id}' RETURN n.id, type(e), m.id"
    res = graph.query(cypher)
    return {"results": res, "node_id": node_id, "type": "explain"}
