import hashlib
import json
import os
import duckdb
from pathlib import Path
from typing import Any, Dict, List
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

def find_repo_root(filepath: str) -> str:
    """Find the root directory of the repository containing the given filepath."""
    filepath = os.path.abspath(filepath)
    current_dir = filepath if os.path.isdir(filepath) else os.path.dirname(filepath)

    while current_dir and current_dir != '/':
        if os.path.isdir(os.path.join(current_dir, '.git')):
            return current_dir
        parent = os.path.dirname(current_dir)
        if parent == current_dir:
            break
        current_dir = parent

    return filepath if os.path.isdir(filepath) else os.path.dirname(filepath)

def get_indexes_dir() -> str:
    """Get the centralized indexes directory."""
    indexes_dir = os.path.expanduser("~/.gitstats3/indexes")
    os.makedirs(indexes_dir, exist_ok=True)
    return indexes_dir

def get_db_path_for_repo(repo_path: str) -> str:
    """Generate a centralized DB path for a specific repository."""
    repo_path = os.path.abspath(repo_path)
    hash_str = hashlib.md5(repo_path.encode('utf-8')).hexdigest()
    return os.path.join(get_indexes_dir(), f"{hash_str}_code_search.duckdb")

def migrate_codebase(db_path: Path):
    """Formal, versioned migration that runs once per DB."""
    with duckdb.connect(str(db_path)) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS code_nodes (
                id VARCHAR PRIMARY KEY,
                name VARCHAR,
                node_type VARCHAR,
                filepath VARCHAR,
                body_text VARCHAR,
                metrics_json VARCHAR,
                start_line INTEGER,
                end_line INTEGER
            )
        ''')

        # Files tracking table for incremental indexing
        conn.execute('''
            CREATE TABLE IF NOT EXISTS files (
                filepath VARCHAR PRIMARY KEY,
                content_hash VARCHAR,
                mtime DOUBLE,
                lang VARCHAR
            )
        ''')

def migrate_all():
    """Scan the indexes directory and safely run migrations."""
    indexes_dir = get_indexes_dir()
    for fname in os.listdir(indexes_dir):
        if fname.endswith(".duckdb"):
            db_path = Path(indexes_dir) / fname
            migrate_codebase(db_path)

class CodeSearchIndex:
    """DuckDB-backed Semantic Code Search Index."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            repo_path = find_repo_root(os.getcwd())
            db_path = get_db_path_for_repo(repo_path)
        self.db_path = db_path
        self._conn = duckdb.connect(self.db_path)
        migrate_codebase(self.db_path)
        self.graph = GorgonzolaGraph(db_path=self.db_path)

    def close(self):
        """Close the persistent DuckDB connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __del__(self):
        self.close()

    def rebuild_fts(self):
        """Rebuild the index."""
        conn = self._conn
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
        # Drop the index if it exists to rebuild
        try:
            conn.execute("PRAGMA drop_fts_index('code_nodes')")
        except Exception:
            pass
        conn.execute("PRAGMA create_fts_index('code_nodes', 'id', 'name', 'body_text')")

    def index_nodes(self, nodes: List[Dict[str, Any]]):
        """Index a batch of AST nodes."""
        conn = self._conn
        data = []
        for n in nodes:
            node_id = f"{n['filepath']}::{n['name']}::{n['start_line']}"
            data.append((
                node_id,
                n['name'],
                n['node_type'],
                n['filepath'],
                n['body_text'],
                json.dumps(n.get('metrics', {})),
                n['start_line'],
                n['end_line']
            ))
        if data:
            conn.executemany('''
                INSERT INTO code_nodes (id, name, node_type, filepath, body_text, metrics_json, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    node_type=excluded.node_type,
                    filepath=excluded.filepath,
                    body_text=excluded.body_text,
                    metrics_json=excluded.metrics_json,
                    start_line=excluded.start_line,
                    end_line=excluded.end_line
            ''', data)

    def clear_file(self, filepath: str):
        """Remove all nodes for a given file before re-indexing it."""
        conn = self._conn
        conn.execute('DELETE FROM code_nodes WHERE filepath = ?', (filepath,))
        conn.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
            
        try:
            self.graph.query_batch([
                "MATCH (f:File {id: $id})-[r1:CONTAINS]->(c:Class)-[r2:CONTAINS]->(m:Method) DETACH DELETE m",
                "MATCH (f:File {id: $id})-[r1:CONTAINS]->(i:Interface)-[r2:CONTAINS]->(m:Method) DETACH DELETE m",
                "MATCH (f:File {id: $id})-[r:CONTAINS]->(child) DETACH DELETE child",
                "MATCH (f:File {id: $id}) DETACH DELETE f",
            ], {"id": filepath})
        except Exception:
            pass

    def upsert_file_hash(self, filepath: str, content_hash: str, mtime: float, lang: str):
        """Upsert a file's hash and metadata for incremental indexing."""
        conn = self._conn
        conn.execute('''
            INSERT INTO files (filepath, content_hash, mtime, lang)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                content_hash=excluded.content_hash,
                mtime=excluded.mtime,
                lang=excluded.lang
        ''', (filepath, content_hash, mtime, lang))
            
        name = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1]
        query = """
            MERGE (f:File {id: $id})
            ON CREATE SET f.name = $name, f.path = $id, f.extension = $ext, f.content_hash = $content_hash, f.mtime = $mtime, f.lang = $lang
            ON MATCH SET f.content_hash = $content_hash, f.mtime = $mtime, f.lang = $lang
        """
        try:
            self.graph.query(query, {
                "id": filepath,
                "name": name,
                "ext": ext,
                "content_hash": content_hash,
                "mtime": float(mtime),
                "lang": lang
            })
        except Exception:
            pass

    def get_file_hash(self, filepath: str) -> str:
        """Retrieve the stored hash for a given file, or None if not found."""
        conn = self._conn
        res = conn.execute('SELECT content_hash FROM files WHERE filepath = ?', (filepath,)).fetchone()
        return res[0] if res else None

    def get_all_tracked_files(self) -> list:
        """Retrieve a list of all filepaths currently tracked in the index."""
        conn = self._conn
        res = conn.execute('SELECT filepath FROM files').fetchall()
        return [row[0] for row in res]

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search the DuckDB FTS index for a match."""
        conn = self._conn
        # Drop query into tokens to make it more FTS-friendly? Just passing query is fine if match_bm25 accepts it.
        # But wait, match_bm25 may raise if index doesn't exist, though we try to ensure it exists.
        try:
            res = conn.execute('''
                SELECT c.name, c.node_type, c.filepath, c.body_text, c.metrics_json, c.start_line, c.end_line,
                       fts_main_code_nodes.match_bm25(c.id, ?) AS score
                FROM code_nodes c
                WHERE fts_main_code_nodes.match_bm25(c.id, ?) IS NOT NULL
                ORDER BY score DESC
                LIMIT ?
            ''', (query, query, limit)).fetchall()
            
            results = []
            for row in res:
                results.append({
                    'name': row[0],
                    'node_type': row[1],
                    'filepath': row[2],
                    'body_text': row[3],
                    'metrics': json.loads(row[4]) if row[4] else {},
                    'start_line': row[5],
                    'end_line': row[6]
                })
            return results
        except Exception:
            return []

    def get_file_nodes(self, filepath: str) -> List[Dict[str, Any]]:
        """Get all nodes for a specific file."""
        conn = self._conn
        res = conn.execute('''
            SELECT name, node_type, filepath, body_text, metrics_json, start_line, end_line
            FROM code_nodes
            WHERE filepath = ?
        ''', (filepath,)).fetchall()
        
        results = []
        for row in res:
            results.append({
                'name': row[0],
                'node_type': row[1],
                'filepath': row[2],
                'body_text': row[3],
                'metrics': json.loads(row[4]) if row[4] else {},
                'start_line': row[5],
                'end_line': row[6]
            })
        return results

    def get_dir_nodes(self, dirpath: str) -> List[Dict[str, Any]]:
        """Get all nodes for files within a specific directory."""
        prefix = dirpath if dirpath.endswith('/') else f"{dirpath}/"
        conn = self._conn
        res = conn.execute('''
            SELECT name, node_type, filepath, body_text, metrics_json, start_line, end_line
            FROM code_nodes
            WHERE filepath LIKE ?
        ''', (f"{prefix}%",)).fetchall()
        
        results = []
        for row in res:
            results.append({
                'name': row[0],
                'node_type': row[1],
                'filepath': row[2],
                'body_text': row[3],
                'metrics': json.loads(row[4]) if row[4] else {},
                'start_line': row[5],
                'end_line': row[6]
            })
        return results
