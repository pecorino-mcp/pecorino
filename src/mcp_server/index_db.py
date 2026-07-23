import functools
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import duckdb

from src.core.errors import SecurityValidationError
from src.mcp_server.config import settings
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

logger = logging.getLogger(__name__)

def find_repo_root(filepath: str, max_depth: int = 20) -> str:
    """Find the root directory of the repository containing the given filepath."""
    path = Path(filepath).resolve()
    current_dir = path if path.is_dir() else path.parent

    visited = set()
    for parent in [current_dir] + list(current_dir.parents):
        real_parent = parent.resolve()
        if real_parent in visited:
            raise SecurityValidationError(f"Symlink loop detected at {parent}")
        visited.add(real_parent)

        if (parent / ".git").is_dir():
            return str(parent)

        if len(visited) > max_depth:
            break

    return str(current_dir)

def get_indexes_dir() -> str:
    """Get the centralized indexes directory."""
    from src.mcp_server.config import settings
    indexes_dir = settings.index_dir
    indexes_dir.mkdir(parents=True, exist_ok=True)
    return str(indexes_dir)

def get_db_path_for_repo(repo_path: str) -> str:
    """Generate a centralized DB path for a specific repository."""
    resolved_repo = Path(repo_path).resolve()
    hash_str = hashlib.md5(str(resolved_repo).encode('utf-8')).hexdigest()
    return str(Path(get_indexes_dir()) / f"{hash_str}_code_search.duckdb")

def get_graph_path_for_repo(duckdb_path: str) -> str:
    """Convert a duckdb file path to the corresponding gorgonzola directory path."""
    p = Path(duckdb_path)
    graph_dir_name = p.stem.replace("_code_search", "_gorgonzola")
    return str(p.parent / graph_dir_name)

def migrate_codebase(conn: duckdb.DuckDBPyConnection):
    """Formal, versioned migration that runs once per DB."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS code_nodes (
            id VARCHAR PRIMARY KEY,
            name VARCHAR,
            kind VARCHAR,
            filepath VARCHAR,
            start_line INTEGER,
            end_line INTEGER
        )
    ''')

    # Run migrations sequentially
    migrations = [
        'ALTER TABLE code_nodes DROP COLUMN body_text',
        'ALTER TABLE code_nodes ADD COLUMN relationships VARCHAR',
        'ALTER TABLE code_nodes DROP COLUMN metrics_json',
        'ALTER TABLE code_nodes ADD COLUMN pagerank DOUBLE DEFAULT 0.0',
        'ALTER TABLE code_nodes ADD COLUMN start_byte INTEGER',
        'ALTER TABLE code_nodes ADD COLUMN end_byte INTEGER',
        'ALTER TABLE code_nodes ADD COLUMN community_id INTEGER',
        f'ALTER TABLE code_nodes ADD COLUMN embedding FLOAT[{settings.embedding_dim}]',
        'ALTER TABLE code_nodes ADD COLUMN complexity INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN signature VARCHAR',
        'ALTER TABLE code_nodes ADD COLUMN in_degree INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN out_degree INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN hcgs_summary VARCHAR',
    ]
    for query in migrations:
        try:
            conn.execute(query)
        except Exception as e:
            logger.debug("Migration query failed (likely already applied): %s", e)

    try:
        conn.execute("CREATE INDEX IF NOT EXISTS code_vss_idx ON code_nodes USING HNSW (embedding)")
    except Exception as e:
        logger.debug("Failed to create HNSW index (might require VSS extension to be fully loaded or data inserted): %s", e)

    # Files tracking table for incremental indexing
    conn.execute('''
        CREATE TABLE IF NOT EXISTS files (
            filepath VARCHAR PRIMARY KEY,
            content_hash VARCHAR,
            mtime DOUBLE,
            lang VARCHAR
        )
    ''')

    # Meta table for FTS tracking and other state
    conn.execute('''
        CREATE TABLE IF NOT EXISTS _meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
    ''')

    # Extension loading
    try:
        try:
            conn.execute("LOAD fts")
        except duckdb.Error:
            conn.execute("INSTALL fts")
            conn.execute("LOAD fts")
            
        try:
            conn.execute("LOAD vss")
        except duckdb.Error:
            conn.execute("INSTALL vss")
            conn.execute("LOAD vss")

        # We don't create the FTS index here anymore. It will be created lazily by ensure_fts().
    except Exception:
        logger.exception("Failed to initialize extensions during migration")

def migrate_all():
    """Scan the indexes directory and safely run migrations."""
    indexes_dir = get_indexes_dir()
    for fname in os.listdir(indexes_dir):
        if fname.endswith(".duckdb"):
            db_path = Path(indexes_dir) / fname
            try:
                with duckdb.connect(str(db_path)) as conn:
                    migrate_codebase(conn)
            except Exception as e:
                logger.warning("Failed to migrate %s: %s", fname, e)

@functools.lru_cache(maxsize=32)
def _get_file_content(filepath: str, mtime: float) -> bytes:
    with open(filepath, 'rb') as f:
        return f.read()

class CodeSearchIndex:
    """DuckDB-backed Semantic Code Search Index."""

    def __init__(self, db_path: str = None, read_only: bool = False):
        self._conn = None
        self.graph = None
        self._embedder = None
        self._read_only = read_only
        if db_path is None:
            repo_path = find_repo_root(os.getcwd())
            db_path = get_db_path_for_repo(repo_path)
        self.db_path = db_path

        try:
            self._conn = duckdb.connect(self.db_path, read_only=read_only)

            # ATTACH other repos for federated querying if we are in read-only mode
            if read_only:
                try:
                    from src.mcp_server.registry import registry
                    for repo in registry.get_all_repos():
                        if repo['duckdb_path'] != self.db_path:
                            try:
                                self._conn.execute(f"ATTACH '{repo['duckdb_path']}' AS repo_{repo['hash']} (READ_ONLY)")
                            except Exception as e:
                                logger.warning(f"Failed to attach repo {repo['name']} for federated query: {e}")
                except Exception as e:
                    logger.debug(f"Could not load registry for ATTACH: {e}")
        except duckdb.IOException as e:
            if not read_only and "not a valid DuckDB database file" in str(e):
                logger.warning("Corrupted DuckDB file detected, removing and recreating: %s", e)
                try:
                    os.remove(self.db_path)
                except OSError:
                    pass
                self._conn = duckdb.connect(self.db_path, read_only=read_only)
            else:
                raise e

        if not read_only:
            migrate_codebase(self._conn)
        else:
            try:
                self._conn.execute("LOAD fts")
            except duckdb.Error:
                try:
                    self._conn.execute("INSTALL fts")
                    self._conn.execute("LOAD fts")
                except Exception:
                    logger.exception("Failed to LOAD/INSTALL fts in read-only mode")
            try:
                self._conn.execute("LOAD vss")
            except duckdb.Error:
                try:
                    self._conn.execute("INSTALL vss")
                    self._conn.execute("LOAD vss")
                except Exception:
                    logger.exception("Failed to LOAD/INSTALL vss in read-only mode")

        if not read_only:
            self.graph = GorgonzolaGraph(db_path=get_graph_path_for_repo(self.db_path))

    def _ensure_graph(self):
        """Lazily initialize the GorgonzolaGraph for write operations."""
        if self.graph is None:
            self.graph = GorgonzolaGraph(db_path=get_graph_path_for_repo(self.db_path))
        return self.graph

    def _get_embedder(self):
        """Lazily initialize the embedding pipeline."""
        if self._embedder is None:
            from src.mcp_server.embedding import EmbeddingPipeline
            self._embedder = EmbeddingPipeline()
        return self._embedder

    def close(self):
        """Close the underlying database connections."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        if getattr(self, 'graph', None):
            try:
                self.graph.close()
            except Exception:
                pass
            self.graph = None

    def __del__(self):
        self.close()

    def rebuild_fts(self):
        """Rebuild the FTS index using DuckDB's overwrite parameter."""
        import logging
        logger = logging.getLogger(__name__)

        conn = self._conn
        try:
            conn.execute("LOAD fts")
        except duckdb.Error:
            conn.execute("INSTALL fts")
            conn.execute("LOAD fts")

        # Let the FTS extension manage the overwrite internally
        # which avoids the catalog dependency bookkeeping bugs
        try:
            conn.execute("PRAGMA create_fts_index('code_nodes', 'id', 'name', 'kind', 'filepath', 'relationships', 'hcgs_summary', overwrite=1)")
        except Exception as e:
            logger.warning("FTS rebuild failed: %s", e)

        try:
            self.clear_fts_dirty()
        except Exception as e:
            logger.warning("FTS rebuilt but failed to clear dirty flag: %s", e)

    def mark_fts_dirty(self):
        """Mark the FTS index as stale (data changed since last rebuild)."""
        try:
            self._conn.execute("""
                INSERT INTO _meta (key, value) VALUES ('fts_dirty', 'true')
                ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = now()
            """)
        except Exception as e:
            logger.warning("Failed to mark FTS dirty (is conn read-only?): %s", e)

    def is_fts_dirty(self) -> bool:
        """Check if the FTS index needs rebuilding."""
        try:
            res = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'fts_dirty'"
            ).fetchone()
            return res is not None and res[0] == 'true'
        except Exception:
            return True  # If we can't tell, assume dirty (defensive, handles migration edge cases)

    def clear_fts_dirty(self):
        """Clear the FTS dirty flag after a successful rebuild."""
        self._conn.execute("""
            INSERT INTO _meta (key, value) VALUES ('fts_dirty', 'false')
            ON CONFLICT(key) DO UPDATE SET value = 'false', updated_at = now()
        """)

    def ensure_fts(self):
        """Rebuild FTS if dirty or missing. Called before search queries."""
        if not self.has_fts_index() or self.is_fts_dirty():
            self.rebuild_fts()

    def has_fts_index(self) -> bool:
        """Check whether the FTS index exists on code_nodes.

        DuckDB's FTS extension creates its internal tables inside a 
        generated schema like fts_main_code_nodes.
        """
        try:
            res = self._conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'fts_main_code_nodes'"
            ).fetchone()
            return res is not None
        except Exception as e:
            logger.warning("Failed to check for FTS index: %s", e)
            return False

    def _lazy_load_body(self, filepath: str, start_line: int, end_line: int, start_byte: int = 0, end_byte: int = 0) -> str:
        """Lazy-load source code from disk using filepath + line range or byte offset."""
        try:
            if start_byte > 0 and end_byte > start_byte:
                mtime = os.path.getmtime(filepath)
                content = _get_file_content(filepath, mtime)
                return content[start_byte:end_byte].decode('utf-8', errors='ignore')
            else:
                with open(filepath, encoding='utf-8', errors='ignore') as f:
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
                n['kind'],
                n['filepath'],
                n['start_line'],
                n['end_line'],
                n.get('relationships', ''),
                0.0,
                n.get('start_byte', 0),
                n.get('end_byte', 0),
                None,  # community_id defaults to None
                n.get('embedding', None),
                n.get('complexity', 0),
                n.get('signature', None),
                0,  # in_degree — computed post-indexing
                0,  # out_degree — computed post-indexing
                n.get('hcgs_summary', None),
            ))
        if data:
            conn.execute("BEGIN TRANSACTION")
            try:
                # Use a temp staging table to avoid row-by-row bind/compile overhead in ON CONFLICT
                conn.execute("CREATE TEMP TABLE temp_code_nodes AS SELECT * FROM code_nodes LIMIT 0")
                conn.executemany("INSERT INTO temp_code_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
                conn.execute('''
                    INSERT INTO code_nodes
                    SELECT * FROM temp_code_nodes
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        kind=excluded.kind,
                        filepath=excluded.filepath,
                        start_line=excluded.start_line,
                        end_line=excluded.end_line,
                        relationships=excluded.relationships,
                        start_byte=excluded.start_byte,
                        end_byte=excluded.end_byte,
                        complexity=excluded.complexity,
                        signature=excluded.signature,
                        hcgs_summary=excluded.hcgs_summary
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
            graph = self._ensure_graph()
            with graph:
                graph.query_batch([
                    "MATCH (f:CodeNode {kind: 'File', id: $id})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:CodeNode {kind: 'Lambda'})-[:ACCESSES_STATE]->(v:CodeNode {kind: 'Variable'}) DETACH DELETE v",
                    "MATCH (f:CodeNode {kind: 'File', id: $id})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:CodeNode {kind: 'Lambda'}) DETACH DELETE l",
                    "MATCH (f:CodeNode {kind: 'File', id: $id})-[:CONTAINS*1..10]->(src)-[:ACCESSES_STATE]->(v:CodeNode {kind: 'Variable'}) DETACH DELETE v",
                    "MATCH (f:CodeNode {kind: 'File', id: $id})-[:CONTAINS*1..10]->(child) DETACH DELETE child",
                    "MATCH (f:CodeNode {kind: 'File', id: $id}) DETACH DELETE f",
                ], {"id": filepath})
        except Exception:
            pass

    def clear_files_bulk(self, filepaths: List[str]):
        """Remove all nodes for a list of files before re-indexing them, in bulk."""
        if not filepaths:
            return
        conn = self._conn
        conn.execute("BEGIN TRANSACTION")
        try:
            chunk_size = 500
            for i in range(0, len(filepaths), chunk_size):
                chunk = filepaths[i:i+chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                conn.execute(f'DELETE FROM code_nodes WHERE filepath IN ({placeholders})', chunk)
                conn.execute(f'DELETE FROM files WHERE filepath IN ({placeholders})', chunk)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise e

        for i in range(0, len(filepaths), chunk_size):
            chunk = filepaths[i:i+chunk_size]
            try:
                graph = self._ensure_graph()
                graph.query_batch([
                    "MATCH (f:CodeNode {kind: 'File'})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:CodeNode {kind: 'Lambda'})-[:ACCESSES_STATE]->(v:CodeNode {kind: 'Variable'}) WHERE f.id IN $ids DETACH DELETE v",
                    "MATCH (f:CodeNode {kind: 'File'})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:CodeNode {kind: 'Lambda'}) WHERE f.id IN $ids DETACH DELETE l",
                    "MATCH (f:CodeNode {kind: 'File'})-[:CONTAINS*1..10]->(src)-[:ACCESSES_STATE]->(v:CodeNode {kind: 'Variable'}) WHERE f.id IN $ids DETACH DELETE v",
                    "MATCH (f:CodeNode {kind: 'File'})-[:CONTAINS*1..10]->(child) WHERE f.id IN $ids DETACH DELETE child",
                    "MATCH (f:CodeNode {kind: 'File'}) WHERE f.id IN $ids DETACH DELETE f",
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

        chunk_size = 500
        for i in range(0, len(files_data), chunk_size):
            chunk = files_data[i:i+chunk_size]
            queries = []
            params = {}
            for j, (filepath, content_hash, mtime, lang) in enumerate(chunk):
                name = os.path.basename(filepath)
                ext = os.path.splitext(filepath)[1]
                q = f"""
                    MERGE (f:File {{id: $id_{j}}})
                    ON CREATE SET f.name = $name_{j}, f.path = $id_{j}, f.extension = $ext_{j}, f.content_hash = $content_hash_{j}, f.mtime = $mtime_{j}, f.lang = $lang_{j}
                    ON MATCH SET f.content_hash = $content_hash_{j}, f.mtime = $mtime_{j}, f.lang = $lang_{j}
                """
                queries.append(q)
                params.update({
                    f"id_{j}": filepath,
                    f"name_{j}": name,
                    f"ext_{j}": ext,
                    f"content_hash_{j}": content_hash,
                    f"mtime_{j}": float(mtime),
                    f"lang_{j}": lang
                })

            if queries:
                try:
                    graph = self._ensure_graph()
                    with graph:
                        graph.query_batch(queries, params)
                except Exception:
                    pass

    def update_summaries_bulk(self, summaries: Dict[str, str]):
        """Update static HCGS summaries for code_nodes in bulk."""
        if not summaries:
            return
        conn = self._conn
        data = [(text, node_id) for node_id, text in summaries.items()]
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.executemany("UPDATE code_nodes SET hcgs_summary = ? WHERE id = ?", data)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise e

    def update_embeddings_bulk(self, pairs: List[Tuple[str, List[float]]]):
        """Update vector embeddings for code_nodes in bulk."""
        if not pairs:
            return
        conn = self._conn
        data = [(emb, node_id) for node_id, emb in pairs]
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.executemany("UPDATE code_nodes SET embedding = ? WHERE id = ?", data)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise e

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
            graph = self._ensure_graph()
            with graph:
                graph.query(query, {
                    "id": filepath,
                    "name": name,
                    "ext": ext,
                    "content_hash": content_hash,
                    "mtime": float(mtime),
                    "lang": lang
                })
        except Exception:
            pass

    def update_pagerank_bulk(self, scores: List[Dict[str, Any]]):
        """Bulk update pagerank scores for code nodes."""
        if not scores:
            return
        conn = self._conn
        conn.execute("BEGIN TRANSACTION")
        try:
            data = []
            for s in scores:
                node_id = s.get("node_id", "")
                score = s.get("score", 0.0)
                if "::" not in node_id:
                    continue # Likely a File node
                parts = node_id.split("::")
                filepath = parts[0]
                if len(parts) == 2:
                    name = parts[1] # Class or Function
                elif len(parts) == 3:
                    name = f"{parts[1]}.{parts[2]}" # Method
                else:
                    continue
                data.append((filepath, name, score))

            conn.execute("CREATE TEMP TABLE temp_pr (filepath VARCHAR, name VARCHAR, pagerank DOUBLE)")
            conn.executemany("INSERT INTO temp_pr VALUES (?, ?, ?)", data)
            conn.execute('''
                UPDATE code_nodes 
                SET pagerank = temp_pr.pagerank 
                FROM temp_pr 
                WHERE code_nodes.filepath = temp_pr.filepath AND code_nodes.name = temp_pr.name
            ''')
            conn.execute("DROP TABLE temp_pr")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.warning("Failed to bulk update pagerank: %s", e)

    def update_community_bulk(self, partitions: List[Dict[str, Any]]):
        """Bulk update the community_id for nodes."""
        if not partitions:
            return
        
        conn = self._conn
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute("CREATE TEMP TABLE temp_comm (id VARCHAR, community_id INTEGER)")
            
            # Insert into temp table
            stmt = "INSERT INTO temp_comm VALUES (?, ?)"
            for p in partitions:
                conn.execute(stmt, (p['node_id'], p['community_id']))
                
            # Update the main table
            conn.execute('''
                UPDATE code_nodes
                SET community_id = temp_comm.community_id
                FROM temp_comm
                WHERE starts_with(code_nodes.id, temp_comm.id)
            ''')
            conn.execute("DROP TABLE temp_comm")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.warning("Failed to bulk update community: %s", e)

    def update_degrees_bulk(self, degree_data: List[Dict[str, Any]]):
        """Bulk update in_degree and out_degree for code nodes.

        degree_data: list of {'name': str, 'in_degree': int, 'out_degree': int}
        """
        if not degree_data:
            return
        conn = self._conn
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute("CREATE TEMP TABLE temp_deg (name VARCHAR, in_deg INTEGER, out_deg INTEGER)")
            data = [(d['name'], d.get('in_degree', 0), d.get('out_degree', 0)) for d in degree_data]
            conn.executemany("INSERT INTO temp_deg VALUES (?, ?, ?)", data)
            conn.execute('''
                UPDATE code_nodes
                SET in_degree = temp_deg.in_deg,
                    out_degree = temp_deg.out_deg
                FROM temp_deg
                WHERE code_nodes.name = temp_deg.name
            ''')
            conn.execute("DROP TABLE temp_deg")
            conn.execute("COMMIT")
            logger.info("Updated in/out degree for %d nodes", len(degree_data))
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.warning("Failed to bulk update degrees: %s", e)

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

    def get_stale_files(self, dirpath: str = None) -> List[str]:
        """Return filepaths where on-disk mtime is newer than indexed mtime.

        Optionally scoped to a directory prefix. This is a cheap stat()-based
        check that avoids content hashing until re-index time.
        """
        conn = self._conn
        if dirpath:
            prefix = dirpath if dirpath.endswith('/') else f"{dirpath}/"
            rows = conn.execute(
                'SELECT filepath, mtime FROM files WHERE filepath LIKE ?',
                (f"{prefix}%",)
            ).fetchall()
        else:
            rows = conn.execute('SELECT filepath, mtime FROM files').fetchall()

        stale = []
        for filepath, indexed_mtime in rows:
            try:
                disk_mtime = os.path.getmtime(filepath)
                if disk_mtime > indexed_mtime + 0.01:  # small epsilon for float comparison
                    stale.append(filepath)
            except OSError:
                pass  # File deleted or inaccessible — stale removal handled elsewhere
        return stale

    def search(self, query: str, limit: int = 10, target_path: str = None, offset: int = 0, mode: str = "fts", boost_ids: list[str] = None) -> List[Dict[str, Any]]:
        """Search the DuckDB FTS index for a match, optionally scoped to a target path."""
        from src.core.errors import AnalysisError, IndexNotFoundError
        from src.mcp_server.config import settings
        
        if mode == "hybrid" and not settings.enable_embeddings:
            logger.warning("Hybrid search requested but embeddings are disabled. Falling back to FTS mode.")
            mode = "fts"

        conn = self._conn
        try:
            # Build path filter clause
            path_filter = ""
            if target_path:
                from src.core.constants import SUPPORTED_EXTENSIONS
                if os.path.splitext(target_path)[1] in SUPPORTED_EXTENSIONS:
                    path_filter = "AND c.filepath = ?"
                else:
                    prefix = target_path if target_path.endswith('/') else f"{target_path}/"
                    path_filter = "AND c.filepath LIKE ?"
            
            if mode == "hybrid":
                # Compute query embedding
                embedder = self._get_embedder()
                q_emb = embedder.embed_batch([query])[0]
                
                # We need array representation for duckdb:
                # But executemany/execute in python handles lists for array parameters natively in duckdb
                boost_clause = ""
                boost_params = []
                if boost_ids:
                    placeholders = ",".join(["?"] * len(boost_ids))
                    boost_clause = f" + CASE WHEN c.id IN ({placeholders}) THEN 100.0 ELSE 0.0 END"
                    boost_params = boost_ids

                # Let's construct the RRF query
                sql = f'''
                    WITH bm25_scores AS (
                        SELECT c.id, 
                               (fts_main_code_nodes.match_bm25(c.id, ?) * (1.0 + COALESCE(c.pagerank, 0.0))) AS bm25_score,
                               row_number() OVER (ORDER BY fts_main_code_nodes.match_bm25(c.id, ?) DESC) as rank_bm25
                        FROM code_nodes c
                        WHERE fts_main_code_nodes.match_bm25(c.id, ?) IS NOT NULL
                        {path_filter}
                        LIMIT 100
                    ),
                    vector_scores AS (
                        SELECT c.id,
                               row_number() OVER (ORDER BY array_cosine_distance(c.embedding, ?::FLOAT[{settings.embedding_dim}]) ASC) as rank_vec
                        FROM code_nodes c
                        WHERE c.embedding IS NOT NULL
                        {path_filter}
                        ORDER BY array_cosine_distance(c.embedding, ?::FLOAT[{settings.embedding_dim}]) ASC
                        LIMIT 100
                    )
                    SELECT c.id, c.name, c.kind, c.filepath, c.start_line, c.end_line, c.start_byte, c.end_byte,
                           COALESCE(b.bm25_score, 0) as bm25_score,
                           ((((1.0 / (60.0 + COALESCE(b.rank_bm25, 100.0))) + (1.0 / (60.0 + COALESCE(v.rank_vec, 100.0))) - 0.0125) / 0.02028688) * (1.0 + COALESCE(c.pagerank, 0.0))){boost_clause} AS score
                    FROM code_nodes c
                    LEFT JOIN bm25_scores b ON c.id = b.id
                    LEFT JOIN vector_scores v ON c.id = v.id
                    WHERE b.id IS NOT NULL OR v.id IS NOT NULL {("OR c.id IN (" + placeholders + ")") if boost_ids else ""}
                    ORDER BY score DESC
                    LIMIT ? OFFSET ?
                '''
                
                params = [query, query, query]
                if target_path:
                    if path_filter == "AND c.filepath = ?":
                        params.append(target_path)
                    else:
                        params.append(f"{prefix}%")
                        
                params.append(q_emb)
                
                if target_path:
                    if path_filter == "AND c.filepath = ?":
                        params.append(target_path)
                    else:
                        params.append(f"{prefix}%")
                        
                params.append(q_emb)
                
                if boost_params:
                    params.extend(boost_params)
                    params.extend(boost_params)
                    
                params.extend([limit, offset])
                
                res = conn.execute(sql, params).fetchall()
            elif mode == "semantic":
                # Compute query embedding
                embedder = self._get_embedder()
                q_emb = embedder.embed_batch([query])[0]
                
                sql = f'''
                    SELECT c.id, c.name, c.kind, c.filepath, c.start_line, c.end_line, c.start_byte, c.end_byte,
                           (1.0 - array_cosine_distance(c.embedding, ?::FLOAT[{settings.embedding_dim}])) AS score
                    FROM code_nodes c
                    WHERE c.embedding IS NOT NULL
                    {path_filter}
                    ORDER BY array_cosine_distance(c.embedding, ?::FLOAT[{settings.embedding_dim}]) ASC
                    LIMIT ? OFFSET ?
                '''
                
                params = [q_emb]
                if target_path:
                    if path_filter == "AND c.filepath = ?":
                        params.append(target_path)
                    else:
                        params.append(f"{prefix}%")
                        
                params.extend([q_emb, limit, offset])
                res = conn.execute(sql, params).fetchall()
            else:
                params = [query, query]
                if target_path:
                    if path_filter == "AND c.filepath = ?":
                        params.append(target_path)
                    else:
                        params.append(f"{prefix}%")
                params.extend([limit, offset])

                # Note: match_bm25 is called twice intentionally. DuckDB FTS requires it in the
                # WHERE clause for filtering and in the SELECT clause to retrieve the score.
                res = conn.execute(f'''
                    SELECT c.id, c.name, c.kind, c.filepath, c.start_line, c.end_line, c.start_byte, c.end_byte,
                           (fts_main_code_nodes.match_bm25(c.id, ?) * (1.0 + COALESCE(c.pagerank, 0.0))) AS score
                    FROM code_nodes c
                    WHERE fts_main_code_nodes.match_bm25(c.id, ?) IS NOT NULL
                    {path_filter}
                    ORDER BY score DESC
                    LIMIT ? OFFSET ?
                ''', params).fetchall()

            results = []
            for row in res:
                entry = {
                    'id': row[0],
                    'name': row[1],
                    'kind': row[2],
                    'filepath': row[3],
                    'body_text': self._lazy_load_body(row[3], row[4], row[5], start_byte=row[6] if row[6] is not None else 0, end_byte=row[7] if row[7] is not None else 0),
                    'start_line': row[4],
                    'end_line': row[5],
                }
                # Surface the BM25×PageRank score (already computed in SQL)
                if len(row) > 8 and row[8] is not None:
                    if mode == "hybrid" and len(row) > 9:
                        entry['bm25_score'] = row[8]
                        entry['score'] = row[9]
                    else:
                        entry['score'] = row[8]
                results.append(entry)

            # Cypher dynamic boost logic
            try:
                graph = self._ensure_graph()
                if graph and results:
                    import math
                    
                    def to_gorgonzola_id(r):
                        filepath = r.get('filepath')
                        name = r.get('name')
                        nt = r.get('kind', '').lower()
                        if not filepath or not name:
                            return r.get('id')
                        if nt == 'method' and '.' in name:
                            parts = name.split('.', 1)
                            return f"{filepath}::{parts[0]}::{parts[1]}"
                        return f"{filepath}::{name}"

                    gorgonzola_to_result = {}
                    for r in results:
                        gorgonzola_to_result[to_gorgonzola_id(r)] = r

                    gorgonzola_ids = list(gorgonzola_to_result.keys())
                    cypher_query = "MATCH ()-[r]->(n:CodeNode) WHERE n.id IN $ids RETURN n.id as id, count(r) AS in_degree"
                    boost_res = graph.query(cypher_query, {"ids": gorgonzola_ids})
                    boost_map = {row['id']: row['in_degree'] for row in boost_res}
                    
                    for k_id, r in gorgonzola_to_result.items():
                        in_deg = boost_map.get(k_id, 0)
                        if 'score' in r and in_deg > 0:
                            r['score'] *= (1.0 + math.log1p(in_deg))
                    results.sort(key=lambda x: x.get('score', 0), reverse=True)

                    # Fetch immediate usages (callers) for top results
                    top_results = results[:5]
                    top_gorgonzola_to_res = {}
                    for r in top_results:
                        top_gorgonzola_to_res[to_gorgonzola_id(r)] = r
                        
                    top_gorgonzola_ids = list(top_gorgonzola_to_res.keys())
                    if top_gorgonzola_ids:
                        usage_query = """
                            MATCH (caller:CodeNode)-[r:CALLS]->(target:CodeNode) 
                            WHERE target.id IN $top_ids 
                            RETURN target.id as target_id, caller.id as caller_id, 
                                   caller.name as caller_name, caller.filepath as filepath, 
                                   caller.start_line as start_line 
                            LIMIT 50
                        """
                        usage_res = graph.query(usage_query, {"top_ids": top_gorgonzola_ids})
                        for row in usage_res:
                            target_id = row['target_id']
                            r = top_gorgonzola_to_res.get(target_id)
                            if r:
                                r.setdefault('usages', []).append({
                                    "caller_id": row['caller_id'],
                                    "name": row['caller_name'],
                                    "filepath": row['filepath'],
                                    "start_line": row['start_line']
                                })

                    for r in results:
                        r.pop('id', None)  # Remove internal ID to avoid cluttering response
            except Exception as e:
                import traceback
                with open("/tmp/pecorino_cypher_error.txt", "w") as f:
                    f.write(traceback.format_exc())
                logger.warning(f"Failed to apply Cypher boost and usages: {e}")

            return results
        except Exception as e:
            err_str = str(e)
            if "fts_main_code_nodes" in err_str or "Catalog Error" in err_str:
                raise IndexNotFoundError(
                    f"Full-text search index has not been built yet. Error details: {err_str}"
                ) from e
            raise AnalysisError(f"FTS query failed: {err_str}") from e

    def get_file_nodes(self, filepath: str) -> List[Dict[str, Any]]:
        """Get all nodes for a specific file."""
        conn = self._conn
        res = conn.execute('''
            SELECT name, kind, filepath, start_line, end_line, start_byte, end_byte,
                   pagerank, complexity, signature, in_degree, out_degree
            FROM code_nodes
            WHERE filepath = ?
        ''', (filepath,)).fetchall()

        results = []
        for row in res:
            results.append({
                'name': row[0],
                'kind': row[1],
                'filepath': row[2],
                'body_text': self._lazy_load_body(row[2], row[3], row[4], start_byte=row[5] if row[5] is not None else 0, end_byte=row[6] if row[6] is not None else 0),
                'start_line': row[3],
                'end_line': row[4],
                'pagerank': row[7] or 0.0,
                'complexity': row[8] or 0,
                'signature': row[9],
                'in_degree': row[10] or 0,
                'out_degree': row[11] or 0,
            })
        return results

    def get_dir_nodes(self, dirpath: str) -> List[Dict[str, Any]]:
        """Get all nodes for files within a specific directory."""
        prefix = dirpath if dirpath.endswith('/') else f"{dirpath}/"
        conn = self._conn
        res = conn.execute('''
            SELECT name, kind, filepath, start_line, end_line, start_byte, end_byte,
                   pagerank, complexity, signature, in_degree, out_degree
            FROM code_nodes
            WHERE filepath LIKE ?
        ''', (f"{prefix}%",)).fetchall()

        results = []
        for row in res:
            results.append({
                'name': row[0],
                'kind': row[1],
                'filepath': row[2],
                'body_text': self._lazy_load_body(row[2], row[3], row[4], start_byte=row[5] if row[5] is not None else 0, end_byte=row[6] if row[6] is not None else 0),
                'start_line': row[3],
                'end_line': row[4],
                'pagerank': row[7] or 0.0,
                'complexity': row[8] or 0,
                'signature': row[9],
                'in_degree': row[10] or 0,
                'out_degree': row[11] or 0,
            })
        return results

    def get_community_nodes(self, community_id: int) -> List[Dict[str, Any]]:
        """Get all nodes belonging to a specific community."""
        conn = self._conn
        res = conn.execute('''
            SELECT name, kind, filepath, start_line, end_line, start_byte, end_byte, pagerank
            FROM code_nodes
            WHERE community_id = ?
            ORDER BY pagerank DESC
        ''', (community_id,)).fetchall()

        results = []
        for row in res:
            results.append({
                'name': row[0],
                'kind': row[1],
                'filepath': row[2],
                'body_text': self._lazy_load_body(row[2], row[3], row[4], start_byte=row[5] if row[5] is not None else 0, end_byte=row[6] if row[6] is not None else 0),
                'metrics': {},
                'start_line': row[3],
                'end_line': row[4],
                'pagerank': row[7]
            })
        return results
