import hashlib
import json
import logging
import os
import duckdb
from pathlib import Path
from typing import Any, Dict, List
from src.core.errors import SecurityValidationError
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
    """Convert a duckdb file path to the corresponding kuzu directory path."""
    p = Path(duckdb_path)
    graph_dir_name = p.stem.replace("_code_search", "_gorgonzola")
    return str(p.parent / graph_dir_name)

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

    # Run migrations sequentially
    migrations = [
        'ALTER TABLE code_nodes DROP COLUMN body_text',
        'ALTER TABLE code_nodes ADD COLUMN relationships VARCHAR',
        'ALTER TABLE code_nodes DROP COLUMN metrics_json'
    ]
    for query in migrations:
        try:
            conn.execute(query)
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

    # Meta table for FTS tracking and other state
    conn.execute('''
        CREATE TABLE IF NOT EXISTS _meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
    ''')

    # FTS schema migration: ensure 4-column FTS index (id, name, node_type, filepath)
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
        # Check if FTS index exists with the expected columns
        fts_info = conn.execute(
            "SELECT sql FROM duckdb_indexes() WHERE index_name = 'fts_main_code_nodes'"
        ).fetchone()
        if fts_info:
            fts_sql = fts_info[0] or ""
            if 'relationships' not in fts_sql:
                try:
                    conn.execute("PRAGMA drop_fts_index('code_nodes')")
                except Exception:
                    conn.execute("DROP SCHEMA IF EXISTS fts_main_code_nodes CASCADE")
                conn.execute("PRAGMA create_fts_index('code_nodes', 'id', 'name', 'node_type', 'filepath', 'relationships')")
    except Exception:
        pass  # FTS not installed or index doesn't exist yet

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

class CodeSearchIndex:
    """DuckDB-backed Semantic Code Search Index."""

    def __init__(self, db_path: str = None, read_only: bool = False):
        self._conn = None
        self.graph = None
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
            except Exception:
                try:
                    self._conn.execute("INSTALL fts")
                    self._conn.execute("LOAD fts")
                except Exception:
                    pass
        
        if not read_only:
            self.graph = GorgonzolaGraph(db_path=get_graph_path_for_repo(self.db_path))

    def _ensure_graph(self):
        """Lazily initialize the GorgonzolaGraph for write operations."""
        if self.graph is None:
            self.graph = GorgonzolaGraph(db_path=get_graph_path_for_repo(self.db_path))
        return self.graph

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
        """Rebuild the FTS index, forcefully cleaning up all stale artifacts first."""
        import logging
        logger = logging.getLogger(__name__)

        conn = self._conn
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")

        # --- Phase 1: Drop via pragma (cleanest path) ---
        try:
            conn.execute("PRAGMA drop_fts_index('code_nodes')")
        except Exception:
            pass

        # --- Phase 2: Force-drop the FTS schema and all its internal tables ---
        try:
            conn.execute("DROP SCHEMA IF EXISTS fts_main_code_nodes CASCADE")
        except Exception:
            pass

        # --- Phase 3: Hunt and destroy orphaned FTS internal tables ---
        # DuckDB's FTS extension creates internal tables (stopwords, docs, fields,
        # etc.) inside its schema. If the schema drop partially fails or the DB
        # was corrupted, these can linger and block the next create_fts_index.
        try:
            orphans = conn.execute("""
                SELECT table_name, schema_name
                FROM information_schema.tables
                WHERE schema_name LIKE 'fts_main_code_nodes%'
            """).fetchall()
            for table_name, schema_name in orphans:
                try:
                    conn.execute(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}" CASCADE')
                except Exception:
                    pass
            # Final attempt to drop the schema if orphans existed
            if orphans:
                try:
                    conn.execute("DROP SCHEMA IF EXISTS fts_main_code_nodes CASCADE")
                except Exception:
                    pass
        except Exception:
            pass  # information_schema query itself failed — DB is very fresh, nothing to clean

        # --- Phase 3.5: Commit cleanup before re-creation ---
        # DuckDB's FTS extension creates internal tables (stopwords, docs, etc.)
        # that are tracked as catalog dependencies. Dropping them and immediately
        # re-creating the FTS index within the same implicit transaction causes a
        # "Could not commit creation of dependency, subject has been deleted" error.
        # Force a commit boundary so the catalog is clean before create_fts_index.
        try:
            conn.execute("CHECKPOINT")
        except Exception:
            pass

        # --- Phase 4: Create fresh index ---
        conn.execute("PRAGMA create_fts_index('code_nodes', 'id', 'name', 'node_type', 'filepath', 'relationships')")

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

        DuckDB's FTS pragma creates internal tables (docs, terms, dict, etc.)
        rather than a regular index visible in duckdb_indexes(). We detect FTS
        by checking for the 'docs' table which is always created.
        """
        try:
            self._conn.execute("LOAD fts")
            res = self._conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'docs'"
            ).fetchone()
            return res is not None
        except (duckdb.CatalogException, duckdb.ParserException, duckdb.IOException):
            return False

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
                n['end_line'],
                n.get('relationships', '')
            ))
        if data:
            conn.execute("BEGIN TRANSACTION")
            try:
                # Use a temp staging table to avoid row-by-row bind/compile overhead in ON CONFLICT
                conn.execute("CREATE TEMP TABLE temp_code_nodes AS SELECT * FROM code_nodes LIMIT 0")
                conn.executemany("INSERT INTO temp_code_nodes VALUES (?, ?, ?, ?, ?, ?, ?)", data)
                conn.execute('''
                    INSERT INTO code_nodes
                    SELECT * FROM temp_code_nodes
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        node_type=excluded.node_type,
                        filepath=excluded.filepath,
                        start_line=excluded.start_line,
                        end_line=excluded.end_line,
                        relationships=excluded.relationships
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
                    "MATCH (f:File {id: $id})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:Lambda)-[:ACCESSES_STATE]->(v:Variable) DETACH DELETE v",
                    "MATCH (f:File {id: $id})-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:Lambda) DETACH DELETE l",
                    "MATCH (f:File {id: $id})-[:CONTAINS*1..10]->(src)-[:ACCESSES_STATE]->(v:Variable) DETACH DELETE v",
                    "MATCH (f:File {id: $id})-[:CONTAINS*1..10]->(child) DETACH DELETE child",
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
                graph = self._ensure_graph()
                graph.query_batch([
                    "MATCH (f:File)-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:Lambda)-[:ACCESSES_STATE]->(v:Variable) WHERE f.id IN $ids DETACH DELETE v",
                    "MATCH (f:File)-[:CONTAINS*1..10]->(src)-[:CONTAINS_LAMBDA*1..3]->(l:Lambda) WHERE f.id IN $ids DETACH DELETE l",
                    "MATCH (f:File)-[:CONTAINS*1..10]->(src)-[:ACCESSES_STATE]->(v:Variable) WHERE f.id IN $ids DETACH DELETE v",
                    "MATCH (f:File)-[:CONTAINS*1..10]->(child) WHERE f.id IN $ids DETACH DELETE child",
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

    def search(self, query: str, limit: int = 10, target_path: str = None, offset: int = 0) -> List[Dict[str, Any]]:
        """Search the DuckDB FTS index for a match, optionally scoped to a target path."""
        from src.core.errors import AnalysisError, IndexNotFoundError
        conn = self._conn
        try:
            # Build path filter clause
            path_filter = ""
            params = [query, query]
            if target_path:
                # Check if target_path looks like a file (has a code extension)
                from src.core.constants import SUPPORTED_EXTENSIONS
                if os.path.splitext(target_path)[1] in SUPPORTED_EXTENSIONS:
                    path_filter = "AND c.filepath = ?"
                    params.append(target_path)
                else:
                    prefix = target_path if target_path.endswith('/') else f"{target_path}/"
                    path_filter = "AND c.filepath LIKE ?"
                    params.append(f"{prefix}%")
            params.extend([limit, offset])

            # Note: match_bm25 is called twice intentionally. DuckDB FTS requires it in the 
            # WHERE clause for filtering and in the SELECT clause to retrieve the score.
            res = conn.execute(f'''
                SELECT c.name, c.node_type, c.filepath, c.start_line, c.end_line,
                       fts_main_code_nodes.match_bm25(c.id, ?) AS score
                FROM code_nodes c
                WHERE fts_main_code_nodes.match_bm25(c.id, ?) IS NOT NULL
                {path_filter}
                ORDER BY score DESC
                LIMIT ? OFFSET ?
            ''', params).fetchall()
            
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
