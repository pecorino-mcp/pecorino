import hashlib
from pathlib import Path
from typing import Dict, List, Optional

import duckdb

from src.mcp_server.config import settings


class RegistryDB:
    """
    Global registry for tracking all indexed repositories across the system.
    This enables federated querying and cross-repository graph traversals.
    """
    def __init__(self):
        self.registry_path = settings.index_dir / "registry.duckdb"
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> duckdb.DuckDBPyConnection:
        # We can open the registry DB in read-write mode briefly since it's a small metadata table
        return duckdb.connect(str(self.registry_path))

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS repositories (
                    hash VARCHAR PRIMARY KEY,
                    repo_path VARCHAR UNIQUE,
                    name VARCHAR,
                    duckdb_path VARCHAR,
                    gorgonzola_path VARCHAR,
                    last_indexed TIMESTAMP DEFAULT now()
                )
            ''')

    def register_repo(self, repo_path: str, duckdb_path: str, gorgonzola_path: str):
        """Register or update a repository in the global registry."""
        resolved = Path(repo_path).resolve()
        hash_str = hashlib.md5(str(resolved).encode('utf-8')).hexdigest()
        name = resolved.name

        with self._get_conn() as conn:
            conn.execute('''
                INSERT INTO repositories (hash, repo_path, name, duckdb_path, gorgonzola_path, last_indexed)
                VALUES (?, ?, ?, ?, ?, now())
                ON CONFLICT (hash) DO UPDATE SET
                    duckdb_path = excluded.duckdb_path,
                    gorgonzola_path = excluded.gorgonzola_path,
                    last_indexed = now()
            ''', (hash_str, str(resolved), name, duckdb_path, gorgonzola_path))

    def get_all_repos(self) -> List[Dict[str, str]]:
        """Get all registered repositories."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT hash, repo_path, name, duckdb_path, gorgonzola_path FROM repositories").fetchall()
            return [
                {
                    "hash": r[0],
                    "repo_path": r[1],
                    "name": r[2],
                    "duckdb_path": r[3],
                    "gorgonzola_path": r[4]
                } for r in rows
            ]

    def get_repo_by_path(self, repo_path: str) -> Optional[Dict[str, str]]:
        resolved = Path(repo_path).resolve()
        hash_str = hashlib.md5(str(resolved).encode('utf-8')).hexdigest()
        with self._get_conn() as conn:
            row = conn.execute("SELECT hash, repo_path, name, duckdb_path, gorgonzola_path FROM repositories WHERE hash = ?", (hash_str,)).fetchone()
            if row:
                return {
                    "hash": row[0],
                    "repo_path": row[1],
                    "name": row[2],
                    "duckdb_path": row[3],
                    "gorgonzola_path": row[4]
                }
            return None

# Singleton instance for the registry
registry = RegistryDB()
