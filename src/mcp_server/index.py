import hashlib
import json
import os
import sqlite3
import fcntl
from pathlib import Path
from typing import Any, Dict, List

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
    return os.path.join(get_indexes_dir(), f"{hash_str}_code_search.db")

def migrate_codebase(db_path: Path):
    """Formal, versioned migration that runs once per DB."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL") # central server = concurrent readers
        # PRAGMA wal_autocheckpoint limits WAL file size
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=67108864") # cap WAL at 64 MB
        
        ver = conn.execute("PRAGMA user_version").fetchone()[0]

        # check for legacy FTS5 (contentless or wrong content table)
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='code_nodes_fts'"
        ).fetchone()

        has_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_nodes'"
        ).fetchone()

        if not has_nodes:
            # We don't have code_nodes table. If we are starting from scratch, that's fine.
            # But if we have fts table without code_nodes, it's a legacy DB.
            if fts_sql:
                raise RuntimeError(f"{db_path.name}: code_nodes missing but fts exists — skip or manually drop legacy DB.")
            else:
                return # Empty database, schema will be created by CodeSearchIndex._init_db

        # v0 -> v1: switch to external-content FTS5
        if ver < 1 or (fts_sql and "content='code_nodes'" not in fts_sql[0]):
            print(f"Migrating {db_path.name} to external-content FTS5...")
            conn.execute("DROP TABLE IF EXISTS code_nodes_fts")
            conn.execute('''
                CREATE VIRTUAL TABLE code_nodes_fts USING fts5(
                    name,
                    node_type,
                    filepath,
                    body_text,
                    content='code_nodes',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                )
            ''')
            # tune for read-heavy central server
            conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('automerge', 8)")
            conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('crisismerge', 32)")
            conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('pgsz', 8192)")
            conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts) VALUES('rebuild')")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
    finally:
        conn.close()

def migrate_all():
    """Scan the indexes directory and safely run migrations with a lock."""
    indexes_dir = get_indexes_dir()
    lock_file_path = os.path.join(indexes_dir, ".migration.lock")
    
    with open(lock_file_path, "w") as lock_file:
        try:
            # Exclusive, non-blocking lock
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Migration already running in another process. Skipping.")
            return

        for path_str in os.listdir(indexes_dir):
            if path_str.endswith(".db"):
                db_path = Path(indexes_dir) / path_str
                try:
                    migrate_codebase(db_path)
                except Exception as e:
                    print(f"FAILED {path_str}: {e}")
                    
        # Unlock is handled automatically when file is closed, but good to be explicit
        fcntl.flock(lock_file, fcntl.LOCK_UN)


class CodeSearchIndex:
    """SQLite FTS5-backed Semantic Code Search Index."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Default to a centralized DB for the current working directory's repository
            repo_path = find_repo_root(os.getcwd())
            db_path = get_db_path_for_repo(repo_path)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Performance pragmas
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA wal_autocheckpoint=1000;")
            conn.execute("PRAGMA journal_size_limit=67108864;") # cap WAL at 64 MB
            
            # Standard SQLite backing table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS code_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    node_type TEXT,
                    filepath TEXT,
                    body_text TEXT,
                    metrics_json TEXT,
                    start_line INTEGER,
                    end_line INTEGER
                )
            ''')
            
            # Files tracking table for incremental indexing
            conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    filepath TEXT PRIMARY KEY,
                    content_hash TEXT,
                    mtime REAL,
                    lang TEXT
                )
            ''')
            
            # FTS5 external content table
            conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS code_nodes_fts USING fts5(
                    name,
                    node_type,
                    filepath,
                    body_text,
                    content='code_nodes',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                )
            ''')
            
            # External content synchronization triggers
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS code_nodes_ai AFTER INSERT ON code_nodes BEGIN
                  INSERT INTO code_nodes_fts(rowid, name, node_type, filepath, body_text) 
                  VALUES (new.id, new.name, new.node_type, new.filepath, new.body_text);
                END;
            ''')
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS code_nodes_ad AFTER DELETE ON code_nodes BEGIN
                  INSERT INTO code_nodes_fts(code_nodes_fts, rowid, name, node_type, filepath, body_text) 
                  VALUES('delete', old.id, old.name, old.node_type, old.filepath, old.body_text);
                END;
            ''')
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS code_nodes_au AFTER UPDATE ON code_nodes BEGIN
                  INSERT INTO code_nodes_fts(code_nodes_fts, rowid, name, node_type, filepath, body_text) 
                  VALUES('delete', old.id, old.name, old.node_type, old.filepath, old.body_text);
                  INSERT INTO code_nodes_fts(rowid, name, node_type, filepath, body_text) 
                  VALUES (new.id, new.name, new.node_type, new.filepath, new.body_text);
                END;
            ''')
            
            # FTS5 Tuning (if it's a new DB we can insert pragmas here too)
            try:
                conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('automerge', 8)")
                conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('crisismerge', 32)")
                conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('pgsz', 8192)")
            except sqlite3.Error:
                pass
            
            # Ensure new DBs have user_version = 1
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver == 0:
                conn.execute("PRAGMA user_version = 1")
                
            conn.commit()

    def index_node(self, name: str, node_type: str, filepath: str, body_text: str, start_line: int, end_line: int, metrics: Dict[str, Any]):
        """Index a single AST node (function, class, etc.)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO code_nodes (name, node_type, filepath, body_text, metrics_json, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (name, node_type, filepath, body_text, json.dumps(metrics), start_line, end_line))
            conn.commit()

    def index_nodes(self, nodes: List[Dict[str, Any]]):
        """Index a batch of AST nodes."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany('''
                INSERT INTO code_nodes (name, node_type, filepath, body_text, metrics_json, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', [
                (n['name'], n['node_type'], n['filepath'], n['body_text'], json.dumps(n.get('metrics', {})), n['start_line'], n['end_line'])
                for n in nodes
            ])
            conn.commit()

    def clear_file(self, filepath: str):
        """Remove all nodes for a given file before re-indexing it."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM code_nodes WHERE filepath = ?', (filepath,))
            conn.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
            conn.commit()

    def upsert_file_hash(self, filepath: str, content_hash: str, mtime: float, lang: str):
        """Upsert a file's hash and metadata for incremental indexing."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO files (filepath, content_hash, mtime, lang)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    mtime=excluded.mtime,
                    lang=excluded.lang
            ''', (filepath, content_hash, mtime, lang))
            conn.commit()

    def get_file_hash(self, filepath: str) -> str:
        """Retrieve the stored hash for a given file, or None if not found."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT content_hash FROM files WHERE filepath = ?', (filepath,))
            row = cursor.fetchone()
            return row[0] if row else None
            
    def get_all_tracked_files(self) -> list:
        """Retrieve a list of all filepaths currently tracked in the index."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT filepath FROM files')
            return [row[0] for row in cursor]

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search the FTS5 index for a match."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                SELECT c.name, c.node_type, c.filepath, c.body_text, c.metrics_json, c.start_line, c.end_line
                FROM code_nodes_fts f
                JOIN code_nodes c ON f.rowid = c.id
                WHERE code_nodes_fts MATCH ?
                ORDER BY f.rank
                LIMIT ?
            ''', (query, limit))

            results = []
            for row in cursor:
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

    def get_file_nodes(self, filepath: str) -> List[Dict[str, Any]]:
        """Get all nodes for a specific file."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                SELECT name, node_type, filepath, body_text, metrics_json, start_line, end_line
                FROM code_nodes
                WHERE filepath = ?
            ''', (filepath,))

            results = []
            for row in cursor:
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
        # Ensure trailing slash for directory matching
        prefix = dirpath if dirpath.endswith('/') else f"{dirpath}/"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                SELECT name, node_type, filepath, body_text, metrics_json, start_line, end_line
                FROM code_nodes
                WHERE filepath LIKE ?
            ''', (f"{prefix}%",))

            results = []
            for row in cursor:
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

    def optimize(self):
        """Run incremental FTS5 optimization merge."""
        with sqlite3.connect(self.db_path) as conn:
            # Incremental merge, does not block the entire database
            conn.execute("INSERT INTO code_nodes_fts(code_nodes_fts, rank) VALUES('merge', 1000)")
            conn.commit()
