import sqlite3
import os
import json
import hashlib
from typing import List, Dict, Any

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

def get_db_path_for_repo(repo_path: str) -> str:
    """Generate a centralized DB path for a specific repository."""
    repo_path = os.path.abspath(repo_path)
    hash_str = hashlib.md5(repo_path.encode('utf-8')).hexdigest()
    indexes_dir = os.path.expanduser("~/.gitstats3/indexes")
    os.makedirs(indexes_dir, exist_ok=True)
    return os.path.join(indexes_dir, f"{hash_str}_code_search.db")

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
            # Create an FTS5 virtual table. Note that metrics_json, start_line, end_line 
            # are marked UNINDEXED so they don't bloat the full-text index but can still be returned.
            conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS code_nodes_fts USING fts5(
                    name,
                    node_type,
                    filepath,
                    body_text,
                    metrics_json UNINDEXED,
                    start_line UNINDEXED,
                    end_line UNINDEXED
                )
            ''')
            conn.commit()

    def index_node(self, name: str, node_type: str, filepath: str, body_text: str, start_line: int, end_line: int, metrics: Dict[str, Any]):
        """Index a single AST node (function, class, etc.)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO code_nodes_fts (name, node_type, filepath, body_text, metrics_json, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (name, node_type, filepath, body_text, json.dumps(metrics), start_line, end_line))
            conn.commit()

    def clear_file(self, filepath: str):
        """Remove all nodes for a given file before re-indexing it."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM code_nodes_fts WHERE filepath = ?', (filepath,))
            conn.commit()

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search the FTS5 index for a match."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                SELECT name, node_type, filepath, body_text, metrics_json, start_line, end_line
                FROM code_nodes_fts
                WHERE code_nodes_fts MATCH ?
                ORDER BY rank
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
