import contextlib
import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from src.mcp_server.config import settings


class RegistryDB:
    """
    Global registry for tracking all indexed repositories across the system.
    This enables federated querying and cross-repository graph traversals.
    """
    def __init__(self):
        self._custom_path: Optional[Path] = None
        self._initialized_path: Optional[Path] = None

    @property
    def registry_path(self) -> Path:
        if self._custom_path is not None:
            return self._custom_path
        return Path(settings.index_dir) / "registry.db"

    @registry_path.setter
    def registry_path(self, value: Path):
        self._custom_path = Path(value) if value is not None else None
        self._initialized_path = None

    @property
    def db_path(self) -> Path:
        return self.registry_path

    @db_path.setter
    def db_path(self, value: Path):
        self.registry_path = value

    @contextlib.contextmanager
    def _get_conn(self):
        current_path = self.registry_path
        current_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(current_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if self._initialized_path != current_path:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS repositories (
                        hash TEXT PRIMARY KEY,
                        repo_path TEXT UNIQUE,
                        name TEXT,
                        db_path TEXT,
                        gorgonzola_path TEXT,
                        last_indexed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.commit()
                self._initialized_path = current_path
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_conn():
            pass

    def register_repo(self, repo_path: str, db_path: str, gorgonzola_path: str):
        """Register or update a repository in the global registry."""
        resolved = Path(repo_path).resolve()
        hash_str = hashlib.md5(str(resolved).encode('utf-8')).hexdigest()
        name = resolved.name

        with self._get_conn() as conn:
            conn.execute('''
                INSERT INTO repositories (hash, repo_path, name, db_path, gorgonzola_path, last_indexed)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(hash) DO UPDATE SET
                    db_path = excluded.db_path,
                    gorgonzola_path = excluded.gorgonzola_path,
                    last_indexed = CURRENT_TIMESTAMP
            ''', (hash_str, str(resolved), name, db_path, gorgonzola_path))
            conn.commit()

    def get_all_repos(self) -> List[Dict[str, str]]:
        """Get all registered repositories."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT hash, repo_path, name, db_path, gorgonzola_path FROM repositories").fetchall()
            return [
                {
                    "hash": r["hash"],
                    "repo_path": r["repo_path"],
                    "name": r["name"],
                    "db_path": r["db_path"],
                    "duckdb_path": r["db_path"],  # Backwards compatibility alias
                    "gorgonzola_path": r["gorgonzola_path"]
                } for r in rows
            ]

    def get_repo_by_path(self, repo_path: str) -> Optional[Dict[str, str]]:
        resolved = Path(repo_path).resolve()
        hash_str = hashlib.md5(str(resolved).encode('utf-8')).hexdigest()
        with self._get_conn() as conn:
            row = conn.execute("SELECT hash, repo_path, name, db_path, gorgonzola_path FROM repositories WHERE hash = ?", (hash_str,)).fetchone()
            if row:
                return {
                    "hash": row["hash"],
                    "repo_path": row["repo_path"],
                    "name": row["name"],
                    "db_path": row["db_path"],
                    "duckdb_path": row["db_path"],  # Backwards compatibility alias
                    "gorgonzola_path": row["gorgonzola_path"]
                }
            return None

# Singleton instance for the registry
registry = RegistryDB()
