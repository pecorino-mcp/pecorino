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
    indexes_dir = os.path.expanduser("~/.pecorino/indexes")
    os.makedirs(indexes_dir, exist_ok=True)
    return indexes_dir

def get_db_path_for_repo(repo_path: str) -> str:
    """Generate a centralized DB path for a specific repository."""
    repo_path = os.path.abspath(repo_path)
    hash_str = hashlib.md5(repo_path.encode('utf-8')).hexdigest()
    return os.path.join(get_indexes_dir(), f"{hash_str}_code_search.duckdb")

def migrate_codebase(conn: duckdb.DuckDBPyConnection):
    """Formal, versioned migration that runs once per DB."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS code_nodes (
            id VARCHAR PRIMARY KEY,
            name VARCHAR,
            node_type VARCHAR,
            filepath VARCHAR,
            start_line INTEGER,
            end_line INTEGER
        )
    ''')

    # Handle existing DBs with old schema that had body_text
    try:
        conn.execute('ALTER TABLE code_nodes DROP COLUMN body_text')
    except Exception:
        pass

    # Handle existing DBs with metrics_json
    try:
        conn.execute('ALTER TABLE code_nodes DROP COLUMN metrics_json')
    except Exception:
        pass

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
            with duckdb.connect(str(db_path)) as conn:
                migrate_codebase(conn)

class CodeSearchIndex:
    """DuckDB-backed Semantic Code Search Index."""

    def __init__(self, db_path: str = None, read_only: bool = False):
        if db_path is None:
            repo_path = find_repo_root(os.getcwd())
            db_path = get_db_path_for_repo(repo_path)
        self.db_path = db_path
        self._conn = duckdb.connect(self.db_path, read_only=read_only)
        if not read_only:
            migrate_codebase(self._conn)
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
        conn.execute("PRAGMA create_fts_index('code_nodes', 'id', 'name')")

    def _lazy_load_body(self, filepath: str, start_line: int, end_line: int) -> str:
        """Lazy-load source code from disk using filepath + line range."""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                return ''.join(lines[max(0, start_line-1):end_line])
        except (FileNotFoundError, OSError):
            return ''

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
                n['start_line'],
                n['end_line']
            ))
        if data:
            conn.execute("BEGIN TRANSACTION")
            try:
                # Use a temp staging table to avoid row-by-row bind/compile overhead in ON CONFLICT
                conn.execute("CREATE TEMP TABLE temp_code_nodes AS SELECT * FROM code_nodes LIMIT 0")
                conn.executemany("INSERT INTO temp_code_nodes VALUES (?, ?, ?, ?, ?, ?)", data)
                conn.execute('''
                    INSERT INTO code_nodes
                    SELECT * FROM temp_code_nodes
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        node_type=excluded.node_type,
                        filepath=excluded.filepath,
                        start_line=excluded.start_line,
                        end_line=excluded.end_line
                ''')
                conn.execute("DROP TABLE temp_code_nodes")
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                raise e

    def clear_file(self, filepath: str):
        """Remove all nodes for a given file before re-indexing it."""
        conn = self._conn
        conn.execute('DELETE FROM code_nodes WHERE filepath = ?', (filepath,))
        conn.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
            
        try:
            with self.graph:
                self.graph.query_batch([
                    "MATCH (f:File {id: $id})-[r1:CONTAINS]->(c:Class)-[r2:CONTAINS]->(m:Method) DETACH DELETE m",
                    "MATCH (f:File {id: $id})-[r1:CONTAINS]->(i:Interface)-[r2:CONTAINS]->(m:Method) DETACH DELETE m",
                    "MATCH (f:File {id: $id})-[r:CONTAINS]->(child) DETACH DELETE child",
                    "MATCH (f:File {id: $id}) DETACH DELETE f",
                ], {"id": filepath})
        except Exception:
            pass

    def clear_files_bulk(self, filepaths: List[str]):
        """Remove all nodes for a list of files before re-indexing them, in bulk."""
        if not filepaths:
            return
        conn = self._conn
        chunk_size = 500
        for i in range(0, len(filepaths), chunk_size):
            chunk = filepaths[i:i+chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            conn.execute(f'DELETE FROM code_nodes WHERE filepath IN ({placeholders})', chunk)
            conn.execute(f'DELETE FROM files WHERE filepath IN ({placeholders})', chunk)
            
        for i in range(0, len(filepaths), chunk_size):
            chunk = filepaths[i:i+chunk_size]
            try:
                self.graph.query_batch([
                    "MATCH (f:File)-[r1:CONTAINS]->(c:Class)-[r2:CONTAINS]->(m:Method) WHERE f.id IN $ids DETACH DELETE m",
                    "MATCH (f:File)-[r1:CONTAINS]->(i:Interface)-[r2:CONTAINS]->(m:Method) WHERE f.id IN $ids DETACH DELETE m",
                    "MATCH (f:File)-[r:CONTAINS]->(child) WHERE f.id IN $ids DETACH DELETE child",
                    "MATCH (f:File) WHERE f.id IN $ids DETACH DELETE f",
                ], {"ids": chunk})
            except Exception:
                pass

    def upsert_file_hashes_bulk(self, files_data: List[tuple]):
        """Upsert a list of file hashes and metadata in bulk."""
        if not files_data:
            return
        conn = self._conn
        conn.execute("BEGIN TRANSACTION")
        try:
            # Use a temp staging table to avoid row-by-row bind/compile overhead in ON CONFLICT
            conn.execute("CREATE TEMP TABLE temp_files AS SELECT * FROM files LIMIT 0")
            conn.executemany("INSERT INTO temp_files VALUES (?, ?, ?, ?)", files_data)
            conn.execute('''
                INSERT INTO files
                SELECT * FROM temp_files
                ON CONFLICT(filepath) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    mtime=excluded.mtime,
                    lang=excluded.lang
            ''')
            conn.execute("DROP TABLE temp_files")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise e
            
        queries = []
        params = {}
        for i, (filepath, content_hash, mtime, lang) in enumerate(files_data):
            name = os.path.basename(filepath)
            ext = os.path.splitext(filepath)[1]
            q = f"""
                MERGE (f:File {{id: $id_{i}}})
                ON CREATE SET f.name = $name_{i}, f.path = $id_{i}, f.extension = $ext_{i}, f.content_hash = $content_hash_{i}, f.mtime = $mtime_{i}, f.lang = $lang_{i}
                ON MATCH SET f.content_hash = $content_hash_{i}, f.mtime = $mtime_{i}, f.lang = $lang_{i}
            """
            queries.append(q)
            params.update({
                f"id_{i}": filepath,
                f"name_{i}": name,
                f"ext_{i}": ext,
                f"content_hash_{i}": content_hash,
                f"mtime_{i}": float(mtime),
                f"lang_{i}": lang
            })
            
        if queries:
            try:
                with self.graph:
                    self.graph.query_batch(queries, params)
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
            with self.graph:
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
                SELECT c.name, c.node_type, c.filepath, c.start_line, c.end_line,
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
                    'body_text': self._lazy_load_body(row[2], row[3], row[4]),
                    'metrics': {},
                    'start_line': row[3],
                    'end_line': row[4]
                })
            return results
        except Exception:
            return []

    def get_file_nodes(self, filepath: str) -> List[Dict[str, Any]]:
        """Get all nodes for a specific file."""
        conn = self._conn
        res = conn.execute('''
            SELECT name, node_type, filepath, start_line, end_line
            FROM code_nodes
            WHERE filepath = ?
        ''', (filepath,)).fetchall()
        
        results = []
        for row in res:
            results.append({
                'name': row[0],
                'node_type': row[1],
                'filepath': row[2],
                'body_text': self._lazy_load_body(row[2], row[3], row[4]),
                'metrics': {},
                'start_line': row[3],
                'end_line': row[4]
            })
        return results

    def get_dir_nodes(self, dirpath: str) -> List[Dict[str, Any]]:
        """Get all nodes for files within a specific directory."""
        prefix = dirpath if dirpath.endswith('/') else f"{dirpath}/"
        conn = self._conn
        res = conn.execute('''
            SELECT name, node_type, filepath, start_line, end_line
            FROM code_nodes
            WHERE filepath LIKE ?
        ''', (f"{prefix}%",)).fetchall()
        
        results = []
        for row in res:
            results.append({
                'name': row[0],
                'node_type': row[1],
                'filepath': row[2],
                'body_text': self._lazy_load_body(row[2], row[3], row[4]),
                'metrics': {},
                'start_line': row[3],
                'end_line': row[4]
            })
        return results
