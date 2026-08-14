
import functools
import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.core.errors import SecurityValidationError
from src.mcp_server.fts_uring_bindings import FTSUringEngine
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

logger = logging.getLogger(__name__)

FTS_URING_LIB_PATH = str(Path(__file__).resolve().parent.parent.parent / "modules" / "c_fts_uring" / "fts_uring.so")

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
    env_dir = os.getenv("PECORINO_INDEX_DIR")
    if env_dir:
        indexes_dir = Path(env_dir).expanduser().resolve()
    else:
        from src.mcp_server.config import settings
        indexes_dir = settings.index_dir
    indexes_dir.mkdir(parents=True, exist_ok=True)
    return str(indexes_dir)

def get_db_path_for_repo(repo_path: str) -> str:
    """Generate a centralized DB path for a specific repository."""
    resolved_repo = Path(repo_path).resolve()
    hash_str = hashlib.md5(str(resolved_repo).encode('utf-8')).hexdigest()
    return str(Path(get_indexes_dir()) / f"{hash_str}_code_search.sqlite3")

def get_graph_path_for_repo(duckdb_path: str) -> str:
    """Convert a duckdb file path to the corresponding gorgonzola directory path."""
    p = Path(duckdb_path)
    graph_dir_name = p.stem.replace("_code_search", "_gorgonzola")
    return str(p.parent / graph_dir_name)

def migrate_codebase(conn: sqlite3.Connection):
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
        'ALTER TABLE code_nodes ADD COLUMN uuid BLOB',
        'ALTER TABLE code_nodes ADD COLUMN complexity INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN signature VARCHAR',
        'ALTER TABLE code_nodes ADD COLUMN in_degree INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN out_degree INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN hcgs_summary VARCHAR',
        # Phase 0: Git features (stable)
        'ALTER TABLE code_nodes ADD COLUMN git_survival_days INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_rename_count INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_ownership_entropy DOUBLE DEFAULT 0.0',
        # Phase 0: Git features (time-dependent)
        'ALTER TABLE code_nodes ADD COLUMN git_commit_count INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_days_since_change INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_churn INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_authors INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN git_bug_fix_ratio DOUBLE DEFAULT 0.0',
        # Phase 0: OOD features
        'ALTER TABLE code_nodes ADD COLUMN instability DOUBLE DEFAULT 0.0',
        'ALTER TABLE code_nodes ADD COLUMN coupling DOUBLE DEFAULT 0.0',
        'ALTER TABLE code_nodes ADD COLUMN depth INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN inheritance_depth INTEGER DEFAULT 0',
        'ALTER TABLE code_nodes ADD COLUMN betweenness DOUBLE DEFAULT 0.0',
    ]
    lib_path = FTS_URING_LIB_PATH
    try:
        conn.enable_load_extension(True)
        conn.load_extension(lib_path)
    except sqlite3.OperationalError as e:
        logger.error(f"Failed to load FTS Uring extension: {e}")

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


def migrate_all():
    """Scan the indexes directory and safely run migrations."""
    indexes_dir = get_indexes_dir()
    for fname in os.listdir(indexes_dir):
        if fname.endswith(".sqlite3"):
            db_path = Path(indexes_dir) / fname
            try:
                with sqlite3.connect(str(db_path)) as conn:
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
        self._fts_engine = None
        self._read_only = read_only
        if db_path is None:
            repo_path = find_repo_root(os.getcwd())
            db_path = get_db_path_for_repo(repo_path)
        self.db_path = db_path

        if self._read_only and not Path(self.db_path).exists():
            from src.core.errors import IndexNotFoundError
            raise IndexNotFoundError(f"Index database not found at {self.db_path}. Run update_index first.")

        try:
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro" if self._read_only else self.db_path,
                uri=True,
                check_same_thread=False
            )
            self._conn.enable_load_extension(True)
            try:
                self._conn.load_extension(FTS_URING_LIB_PATH)
            except Exception as e:
                logger.error(f"Failed to load fts_uring.so: {e}")

            fts_path = str(Path(self.db_path).parent / f"{Path(self.db_path).stem}_fts")
            try:
                self._fts_engine = FTSUringEngine(FTS_URING_LIB_PATH)
            except Exception as e:
                logger.error(f"Failed to init FTSUringEngine: {e}")

            import sys

            from src.mcp_server.config import settings
            max_ram = getattr(settings, 'fts_ram_mb', 256)
            if 'pytest' in sys.modules:
                max_ram = 128
            self._conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS pecorino_ast USING fts_uring('{fts_path}', {max_ram});")
            # Force SQLite to call xConnect to initialize g_engine in the C extension
            try:
                self._conn.execute("SELECT 1 FROM pecorino_ast WHERE query = 'init' LIMIT 1")
            except Exception as e:
                logger.error(f"Error querying pecorino_ast during init: {e}")

            # ATTACH other repos for federated querying if we are in read-only mode (max 9 to respect SQLite 10 DB limit)
            if read_only:
                try:
                    from src.mcp_server.registry import registry
                    attached_count = 0
                    for repo in registry.get_all_repos():
                        if attached_count >= 9:
                            break
                        duck_path = repo.get('duckdb_path')
                        if duck_path and duck_path != self.db_path and Path(duck_path).exists():
                            try:
                                self._conn.execute(f"ATTACH '{duck_path}' AS repo_{repo['hash']}")
                                attached_count += 1
                            except Exception as e:
                                logger.warning(f"Failed to attach repo {repo.get('name')} for federated query: {e}")
                except Exception as e:
                    logger.debug(f"Could not load registry for ATTACH: {e}")
        except sqlite3.OperationalError as e:
            raise e

        if not read_only:
            migrate_codebase(self._conn)

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
            if self._fts_engine:
                try:
                    self._fts_engine.close()
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
        """Rebuild the FTS index."""
        try:
            self._conn.execute("""
                INSERT INTO _meta (key, value) VALUES ('fts_built', 'true')
                ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = CURRENT_TIMESTAMP
            """)
            self.clear_fts_dirty()
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to rebuild FTS index: %s", e)

    def mark_fts_dirty(self):
        """Mark the FTS index as stale (data changed since last rebuild)."""
        try:
            self._conn.execute("""
                INSERT INTO _meta (key, value) VALUES ('fts_dirty', 'true')
                ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = CURRENT_TIMESTAMP
            """)
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to mark FTS dirty (is conn read-only?): %s", e)

    def is_fts_dirty(self) -> bool:
        """Check if the FTS index needs rebuilding."""
        try:
            row = self._conn.execute("SELECT value FROM _meta WHERE key = 'fts_dirty'").fetchone()
            return row is not None and row[0] == 'true'
        except Exception:
            return False

    def clear_fts_dirty(self):
        """Clear the FTS dirty flag after a successful rebuild."""
        try:
            self._conn.execute("""
                INSERT INTO _meta (key, value) VALUES ('fts_dirty', 'false')
                ON CONFLICT(key) DO UPDATE SET value = 'false', updated_at = CURRENT_TIMESTAMP
            """)
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to clear FTS dirty: %s", e)

    def ensure_fts(self):
        """Rebuild FTS if dirty or missing. Called before search queries."""
        if self.is_fts_dirty() or not self.has_fts_index():
            self.rebuild_fts()

    def has_fts_index(self) -> bool:
        """Check if the fts index exists and has been built."""
        try:
            # Check table existence first
            row_table = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pecorino_ast'"
            ).fetchone()
            if row_table is None:
                return False
            # Check build metadata flag
            row_built = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'fts_built'"
            ).fetchone()
            return row_built is not None and row_built[0] == 'true'
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
            node_id = n.get('id', f"{n['filepath']}::{n['name']}::{n['start_line']}")

            # MD5 hash node_id to get 16 bytes for fts_uring
            import hashlib
            node_uuid = hashlib.md5(node_id.encode('utf-8')).digest()

            # Dummy TF and DL for now to pass into fts_uring
            tf = [1, 0, 0, 0]
            dl = [len(n.get('name', '')) or 1, 0, 0, 0]
            embedding = n.get('embedding', None)

            if self._fts_engine:
                try:
                    text = f"{n.get('name', '')} {n.get('kind', '')} {n.get('filepath', '')}"
                    res = self._fts_engine.insert_document(node_uuid, tf, dl, embedding, text)
                    if res != 0:
                        logger.error(f"insert_document returned {res}")
                except Exception as e:
                    logger.warning(f"Failed to insert into fts_uring: {e}")

            data.append((
                node_id,
                node_uuid,
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
                n.get('complexity', 0),
                n.get('signature', None),
                0,  # in_degree — computed post-indexing
                0,  # out_degree — computed post-indexing
                n.get('hcgs_summary', None),
            ))
        if data:
            pass
            try:
                insert_cols = (
                    "id, uuid, name, kind, filepath, start_line, end_line, "
                    "relationships, pagerank, start_byte, end_byte, "
                    "community_id, complexity, signature, "
                    "in_degree, out_degree, hcgs_summary"
                )
                placeholders = ", ".join(["?"] * 17)
                conn.executemany(f'''
                    INSERT INTO code_nodes ({insert_cols})
                    VALUES ({placeholders})
                    ON CONFLICT(id) DO UPDATE SET
                        uuid=excluded.uuid,
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
                ''', data)
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise e

    def clear_file(self, filepath: str):
        """Remove all nodes for a given file before re-indexing it."""
        conn = self._conn
        conn.execute('DELETE FROM code_nodes WHERE filepath = ?', (filepath,))
        conn.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
        conn.commit()

        try:
            graph = self._ensure_graph()
            with graph:
                self._clear_graph_nodes(graph, [filepath])
        except Exception:
            pass

    def clear_files_bulk(self, filepaths: List[str]):
        """Remove all nodes for a list of files before re-indexing them, in bulk."""
        if not filepaths:
            return
        conn = self._conn
        pass
        try:
            chunk_size = 500
            for i in range(0, len(filepaths), chunk_size):
                chunk = filepaths[i:i+chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                conn.execute(f'DELETE FROM code_nodes WHERE filepath IN ({placeholders})', chunk)
                conn.execute(f'DELETE FROM files WHERE filepath IN ({placeholders})', chunk)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e

        # Graph cleanup — chunked, using only single-hop queries to avoid
        # the SIGSEGV in Gorgonzola's RecursiveExtend/PathPropertyProbe
        # that triggers on variable-length patterns (*0..10, *1..10).
        chunk_size = 200
        for i in range(0, len(filepaths), chunk_size):
            chunk = filepaths[i:i+chunk_size]
            try:
                graph = self._ensure_graph()
                with graph:
                    self._clear_graph_nodes(graph, chunk)
            except Exception:
                pass

    def _clear_graph_nodes(self, graph, file_ids: List[str]):
        """Remove graph nodes for the given file IDs using iterative single-hop
        traversal. Avoids variable-length path patterns (*0..N) which trigger
        a SIGSEGV in the current Gorgonzola engine."""
        # 1. Collect all descendant node IDs via iterative single-hop CONTAINS.
        #    Max depth 8 is generous for AST-like structures (real depth is 3-6).
        to_delete = set()
        current_ids = set(file_ids)
        max_depth = 8

        for _ in range(max_depth):
            if not current_ids:
                break
            rows = graph.query(
                "MATCH (n:CodeNode)-[:CONTAINS]->(c:CodeNode) "
                "WHERE n.id IN $ids RETURN c.id",
                {"ids": list(current_ids)})
            next_level = {r[0] for r in rows} - to_delete
            to_delete.update(next_level)
            current_ids = next_level

        # Include the file nodes themselves in the set for side-branch collection
        all_ids = to_delete | set(file_ids)

        # 2. Collect side-branch nodes: lambdas (up to 3 hops deep)
        lambdas = set()
        lambda_sources = all_ids
        for _ in range(3):
            if not lambda_sources:
                break
            rows = graph.query(
                "MATCH (src:CodeNode)-[:CONTAINS_LAMBDA]->(l:CodeNode {kind: 'Lambda'}) "
                "WHERE src.id IN $ids RETURN l.id",
                {"ids": list(lambda_sources)})
            new_lambdas = {r[0] for r in rows} - lambdas
            lambdas.update(new_lambdas)
            lambda_sources = new_lambdas
        to_delete.update(lambdas)

        # 3. Collect variables accessed from any of the collected nodes
        access_sources = all_ids | lambdas
        if access_sources:
            rows = graph.query(
                "MATCH (src:CodeNode)-[:ACCESSES_STATE]->(v:CodeNode {kind: 'Variable'}) "
                "WHERE src.id IN $ids RETURN v.id",
                {"ids": list(access_sources)})
            to_delete.update(r[0] for r in rows)

        # 4. Delete HAS_IDENTIFIER edges from all collected nodes
        all_to_clean = to_delete | set(file_ids)
        if all_to_clean:
            graph.query(
                "MATCH (c:CodeNode)-[r:HAS_IDENTIFIER]->(i:Identifier) "
                "WHERE c.id IN $ids DELETE r",
                {"ids": list(all_to_clean)})

        # 5. DETACH DELETE all collected descendant nodes
        if to_delete:
            graph.query(
                "MATCH (n:CodeNode) WHERE n.id IN $ids DETACH DELETE n",
                {"ids": list(to_delete)})

        # 6. DETACH DELETE the File nodes themselves
        graph.query(
            "MATCH (f:CodeNode {kind: 'File'}) WHERE f.id IN $ids DETACH DELETE f",
            {"ids": file_ids})

        graph.purge_orphaned_identifiers()

    def upsert_file_hashes_bulk(self, files_data: List[tuple]):
        """Upsert a list of file hashes and metadata in bulk."""
        if not files_data:
            return
        conn = self._conn
        pass
        try:
            conn.executemany('''
                INSERT INTO files (filepath, content_hash, mtime, lang)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    mtime=excluded.mtime,
                    lang=excluded.lang
            ''', files_data)
            conn.commit()
        except Exception as e:
            conn.rollback()
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
        pass
        try:
            conn.executemany("UPDATE code_nodes SET hcgs_summary = ? WHERE id = ?", data)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e

    def update_embeddings_bulk(self, pairs: List[Tuple[str, List[float]]]):
        """Update vector embeddings for code_nodes in bulk."""
        if not pairs:
            return
        conn = self._conn
        import pandas as pd
        pd.DataFrame([(node_id, emb) for node_id, emb in pairs], columns=["id", "embedding"])
        pass
        try:
            conn.execute("""
                UPDATE code_nodes
                SET embedding = df.embedding
                FROM df
                WHERE code_nodes.id = df.id
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
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
        conn.commit()

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
        pass
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
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("Failed to bulk update pagerank: %s", e)

    def update_community_bulk(self, partitions: List[Dict[str, Any]]):
        """Bulk update the community_id for nodes."""
        if not partitions:
            return

        conn = self._conn
        try:
            pass
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
                WHERE code_nodes.id LIKE temp_comm.id || '%'
            ''')
            conn.execute("DROP TABLE temp_comm")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("Failed to bulk update community: %s", e)

    def update_degrees_bulk(self, degree_data: List[Dict[str, Any]]):
        """Bulk update in_degree and out_degree for code nodes.

        degree_data: list of {'name': str, 'in_degree': int, 'out_degree': int}
        """
        if not degree_data:
            return
        conn = self._conn
        try:
            pass
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
            conn.commit()
            logger.info("Updated in/out degree for %d nodes", len(degree_data))
        except Exception as e:
            conn.rollback()
            logger.warning("Failed to bulk update degrees: %s", e)

    def update_git_features_bulk(self, git_features: List[Dict[str, Any]]):
        """Bulk update git temporal and stable metrics for code nodes matching filepath.

        git_features: list of dicts with keys:
            'filepath', 'git_survival_days', 'git_rename_count', 'git_ownership_entropy',
            'git_commit_count', 'git_days_since_change', 'git_churn', 'git_authors', 'git_bug_fix_ratio'
        """
        if not git_features:
            return
        conn = self._conn
        try:
            pass
            conn.execute("""
                CREATE TEMP TABLE temp_git (
                    filepath VARCHAR,
                    git_survival_days INTEGER,
                    git_rename_count INTEGER,
                    git_ownership_entropy DOUBLE,
                    git_commit_count INTEGER,
                    git_days_since_change INTEGER,
                    git_churn INTEGER,
                    git_authors INTEGER,
                    git_bug_fix_ratio DOUBLE
                )
            """)
            data = [
                (
                    f.get("filepath", ""),
                    int(f.get("git_survival_days", 0)),
                    int(f.get("git_rename_count", 0)),
                    float(f.get("git_ownership_entropy", 0.0)),
                    int(f.get("git_commit_count", 0)),
                    int(f.get("git_days_since_change", 0)),
                    int(f.get("git_churn", 0)),
                    int(f.get("git_authors", 0)),
                    float(f.get("git_bug_fix_ratio", 0.0)),
                )
                for f in git_features
            ]
            conn.executemany("INSERT INTO temp_git VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
            conn.execute("""
                UPDATE code_nodes
                SET git_survival_days = temp_git.git_survival_days,
                    git_rename_count = temp_git.git_rename_count,
                    git_ownership_entropy = temp_git.git_ownership_entropy,
                    git_commit_count = temp_git.git_commit_count,
                    git_days_since_change = temp_git.git_days_since_change,
                    git_churn = temp_git.git_churn,
                    git_authors = temp_git.git_authors,
                    git_bug_fix_ratio = temp_git.git_bug_fix_ratio
                FROM temp_git
                WHERE code_nodes.filepath = temp_git.filepath
            """)
            conn.execute("DROP TABLE temp_git")
            conn.commit()
            logger.info("Updated git features for %d files", len(git_features))
        except Exception as e:
            conn.rollback()
            logger.warning("Failed to bulk update git features: %s", e)

    def update_ood_features_bulk(self, ood_data: List[Dict[str, Any]]):
        """Bulk update Object-Oriented Design (OOD) features for code nodes matching id.

        ood_data: list of dicts with keys:
            'id', 'instability', 'coupling', 'depth', 'inheritance_depth', 'betweenness'
        """
        if not ood_data:
            return
        conn = self._conn
        try:
            pass
            conn.execute("""
                CREATE TEMP TABLE temp_ood (
                    id VARCHAR,
                    instability DOUBLE,
                    coupling DOUBLE,
                    depth INTEGER,
                    inheritance_depth INTEGER,
                    betweenness DOUBLE
                )
            """)
            data = [
                (
                    item.get("id", ""),
                    float(item.get("instability", 0.0)),
                    float(item.get("coupling", 0.0)),
                    int(item.get("depth", 0)),
                    int(item.get("inheritance_depth", 0)),
                    float(item.get("betweenness", 0.0)),
                )
                for item in ood_data
            ]
            conn.executemany("INSERT INTO temp_ood VALUES (?, ?, ?, ?, ?, ?)", data)
            conn.execute("""
                UPDATE code_nodes
                SET instability = temp_ood.instability,
                    coupling = temp_ood.coupling,
                    depth = temp_ood.depth,
                    inheritance_depth = temp_ood.inheritance_depth,
                    betweenness = temp_ood.betweenness
                FROM temp_ood
                WHERE code_nodes.id = temp_ood.id
            """)
            conn.execute("DROP TABLE temp_ood")
            conn.commit()
            logger.info("Updated OOD features for %d nodes", len(ood_data))
        except Exception as e:
            conn.rollback()
            logger.warning("Failed to bulk update OOD features: %s", e)

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

    def search(self, query: str, limit: int = 10, target_path: str = None, offset: int = 0, mode: str = "fts", boost_ids: list[str] = None, explain: bool = False) -> List[Dict[str, Any]]:
        """Search the SQLite FTS index backed by fts_uring."""
        import sqlite3

        conn = self._conn
        try:
            path_filter = ""
            params = [query]
            if target_path:
                from src.core.constants import SUPPORTED_EXTENSIONS
                if os.path.splitext(target_path)[1] in SUPPORTED_EXTENSIONS:
                    path_filter = "AND c.filepath = ?"
                    params.append(target_path)
                else:
                    prefix = target_path if target_path.endswith('/') else f"{target_path}/"
                    path_filter = "AND c.filepath LIKE ?"
                    params.append(f"{prefix}%")

            boost_clause = ""
            if boost_ids:
                placeholders = ",".join(["?"] * len(boost_ids))
                boost_clause = f" + CASE WHEN c.id IN ({placeholders}) THEN 100.0 ELSE 0.0 END"
                params.extend(boost_ids)

            sql = f'''
                SELECT c.id, c.name, c.kind, c.filepath, c.start_line, c.end_line, c.start_byte, c.end_byte,
                       a.bm25f_score AS bm25_score,
                       ((a.rrf_score) * (1.0 + COALESCE(c.pagerank, 0.0))){boost_clause} AS score,
                       a.cosine_score AS vec_sim
                FROM pecorino_ast a
                JOIN code_nodes c ON lower(hex(c.uuid)) = a.node_id
                WHERE a.query MATCH ?
                {path_filter}
                ORDER BY score DESC
                LIMIT ? OFFSET ?
            '''

            params.extend([limit, offset])

            # Use dict cursor equivalent
            conn.row_factory = sqlite3.Row
            res = conn.execute(sql, params).fetchall()
            conn.row_factory = None

            results = []
            for row in res:
                r = dict(row)
                r['id']
                r['hcgs_summary'] = ''

                if explain:
                    r['explanation'] = "Score computed via fts_uring RRF + PageRank."

                results.append(r)

            return results

        except sqlite3.Error as e:
            import logging
            logging.getLogger(__name__).error("Search failed: %s", e)
            return []

    def get_file_nodes(self, filepath: str) -> List[Dict[str, Any]]:
        """Retrieve all indexed code nodes for a specific file."""
        if not self._conn:
            return []
        try:
            cursor = self._conn.execute(
                "SELECT id, name, kind, filepath, start_line, end_line, start_byte, end_byte, signature FROM code_nodes WHERE filepath = ?",
                (filepath,)
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get_file_nodes for {filepath}: {e}")
            return []

    def get_dir_nodes(self, dirpath: str) -> List[Dict[str, Any]]:
        """Retrieve all indexed code nodes within a directory tree."""
        if not self._conn:
            return []
        try:
            normalized_dir = dirpath if dirpath.endswith(os.sep) else dirpath + os.sep
            cursor = self._conn.execute(
                "SELECT id, name, kind, filepath, start_line, end_line, start_byte, end_byte, signature FROM code_nodes WHERE filepath LIKE ? OR filepath = ?",
                (normalized_dir + "%", dirpath)
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get_dir_nodes for {dirpath}: {e}")
            return []

