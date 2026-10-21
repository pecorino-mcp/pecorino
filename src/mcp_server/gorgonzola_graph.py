import contextlib
import csv
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Column orders for each node table (kept for compatibility with callers inspecting schema)
_NODE_COLUMNS = {
    "CodeNode": [
        "id", "kind", "name", "qualified_name", "file",
        "line", "end_line", "mtime", "complexity", "docstring", "embedding",
    ],
    "Identifier": [
        "id", "raw", "tokens", "case_style", "prefix", "suffix",
        "verb", "entity", "qualifier", "is_magic", "canonical_verb",
        "canonical_entity", "domain", "intent", "embedding",
    ],
}

_NUMERIC_COLUMNS = {"complexity", "line", "end_line", "mtime"}

_RELATIONSHIP_SCHEMA = [
    "CREATE REL TABLE HAS_IDENTIFIER (FROM CodeNode TO Identifier)",
    "CREATE REL TABLE CONTAINS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE CALLS (FROM CodeNode TO CodeNode, line INT64)",
    "CREATE REL TABLE IMPORTS (FROM CodeNode TO CodeNode, is_external BOOLEAN, import_text STRING)",
    "CREATE REL TABLE INHERITS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE PARAMETER_OF (FROM CodeNode TO CodeNode, position INT64)",
    "CREATE REL TABLE RETURNS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE DEPENDS_ON (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE DEFINES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE EXTENDS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE IMPLEMENTS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE FILE_CHANGES_WITH (FROM File TO File, weight DOUBLE)",
    "CREATE REL TABLE RAISES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE TESTS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE HTTP_CALLS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE READS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE WRITES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE HAS_PARAMETER (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE USES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE CONTAINS_LAMBDA (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE ACCESSES_STATE (FROM CodeNode TO CodeNode, is_read BOOLEAN, is_mutation BOOLEAN, is_taint BOOLEAN)",
    "CREATE REL TABLE RECURSES_TO (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE SEMANTICALLY_RELATED (FROM CodeNode TO CodeNode, weight DOUBLE)",
    "CREATE REL TABLE SIMILAR_TO (FROM CodeNode TO CodeNode, score DOUBLE)",
]

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'CodeNode',
    name TEXT DEFAULT '',
    qualified_name TEXT DEFAULT '',
    file TEXT DEFAULT '',
    filepath TEXT DEFAULT '',
    path TEXT DEFAULT '',
    extension TEXT DEFAULT '',
    content_hash TEXT DEFAULT '',
    lang TEXT DEFAULT '',
    line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0.0,
    complexity INTEGER DEFAULT 0,
    docstring TEXT DEFAULT '',
    embedding TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind ON graph_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_file ON graph_nodes(file);

CREATE TABLE IF NOT EXISTS graph_identifiers (
    id TEXT PRIMARY KEY,
    raw TEXT DEFAULT '',
    tokens TEXT DEFAULT '',
    case_style TEXT DEFAULT '',
    prefix TEXT DEFAULT '',
    suffix TEXT DEFAULT '',
    verb TEXT DEFAULT '',
    entity TEXT DEFAULT '',
    qualifier TEXT DEFAULT '',
    is_magic INTEGER DEFAULT 0,
    canonical_verb TEXT DEFAULT '',
    canonical_entity TEXT DEFAULT '',
    domain TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    embedding TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_graph_ident_canonical ON graph_identifiers(canonical_verb, canonical_entity);

CREATE TABLE IF NOT EXISTS graph_edges (
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0,
    weight REAL DEFAULT 0.0,
    score REAL DEFAULT 0.0,
    confidence REAL DEFAULT 0.0,
    is_external INTEGER DEFAULT 0,
    import_text TEXT DEFAULT '',
    is_read INTEGER DEFAULT 0,
    is_mutation INTEGER DEFAULT 0,
    is_taint INTEGER DEFAULT 0,
    PRIMARY KEY (src_id, dst_id, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(rel_type, src_id, dst_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(rel_type, dst_id, src_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst_only ON graph_edges(dst_id);
"""


class _QueryResultWrapper:
    """Compatibility cursor wrapper supporting both Kùzu-style has_next()/get_next() and SQLite fetchall()."""

    def __init__(self, rows: List[Any], columns: Optional[List[str]] = None):
        self._rows = list(rows)
        self._columns = list(columns or [])
        self._idx = 0
        self._as_dict = False

    def rows_as_dict(self, flag: bool = True) -> None:
        self._as_dict = flag

    def has_next(self) -> bool:
        return self._idx < len(self._rows)

    def get_next(self) -> Any:
        row = self._rows[self._idx]
        self._idx += 1
        if self._as_dict:
            if isinstance(row, dict):
                return row
            return {
                (self._columns[i] if i < len(self._columns) else str(i)): val
                for i, val in enumerate(row)
            }
        if isinstance(row, dict):
            if self._columns:
                return [row.get(c) for c in self._columns]
            return list(row.values())
        return list(row) if isinstance(row, tuple) else row

    def get_column_names(self) -> List[str]:
        return self._columns

    def fetchall(self) -> List[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class _SQLiteGraphConnection:
    """Thin wrapper over sqlite3.Connection that also intercepts Cypher/Kùzu commands."""

    def __init__(self, graph: "GorgonzolaGraph", raw_conn: sqlite3.Connection):
        self._graph = graph
        self.raw_conn = raw_conn

    def execute(self, query: str, parameters: Any = None) -> _QueryResultWrapper:
        q_strip = (query or "").strip()
        q_upper = q_strip.upper()

        if not q_strip:
            return _QueryResultWrapper([])

        if q_upper.startswith("CHECKPOINT"):
            try:
                self.raw_conn.commit()
                self.raw_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            return _QueryResultWrapper([])

        if q_upper.startswith(("CALL ", "LOAD EXTENSION", "CREATE NODE TABLE", "CREATE REL TABLE")):
            if "SHOW_TABLES" in q_upper:
                tables = [[i, name] for i, name in enumerate(["CodeNode", "Identifier", "File"])]
                return _QueryResultWrapper(tables, ["id", "name"])
            if "LEIDEN" in q_upper:
                comms = self._graph.compute_leiden_communities()
                col_id = "node_id" if "AS NODE_ID" in q_upper else "id"
                rows = [{col_id: k, "leiden_id": v} for k, v in comms.items()]
                return _QueryResultWrapper(rows, [col_id, "leiden_id"])
            if "PAGE_RANK" in q_upper:
                prs = self._graph.pagerank()
                rows = [{"id": r["node_id"], "rank": r["score"]} for r in prs]
                return _QueryResultWrapper(rows, ["id", "rank"])
            return _QueryResultWrapper([])

        if q_upper.startswith("COPY "):
            self._graph._handle_copy_statement(q_strip, self.raw_conn)
            return _QueryResultWrapper([])

        if q_upper.startswith(("MATCH ", "MERGE ", "OPTIONAL MATCH ", "WITH ")):
            rows = self._graph._execute_cypher(q_strip, parameters or {}, self.raw_conn)
            cols = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
            return _QueryResultWrapper(rows, cols)

        # Native SQLite SQL
        if parameters is not None:
            cur = self.raw_conn.execute(q_strip, parameters)
        else:
            cur = self.raw_conn.execute(q_strip)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
        return _QueryResultWrapper(rows, cols)

    def commit(self) -> None:
        self.raw_conn.commit()

    def close(self) -> None:
        self.raw_conn.close()


def init_gorgonzola_schema(conn) -> None:
    raw = conn.raw_conn if isinstance(conn, _SQLiteGraphConnection) else conn
    raw.executescript(_SQLITE_SCHEMA)
    raw.commit()


class GorgonzolaGraph:
    """SQLite3-backed property graph engine for Pecorino (drop-in replacement for Kùzu/Gorgonzola)."""

    def __init__(self, db_path: str):
        self._label_cache: Dict[str, str] = {}
        self._label_cache_lock = threading.Lock()
        self._db = None
        self._conn: Optional[_SQLiteGraphConnection] = None
        self._raw_conn: Optional[sqlite3.Connection] = None
        self._in_context = False
        self._context_depth = 0
        self._schema_initialized = False

        from src.mcp_server.index_db import get_graph_path_for_repo
        self.gorgonzola_db_path = (
            get_graph_path_for_repo(db_path)
            if any(db_path.endswith(ext) for ext in (".sqlite3", ".sqlite", ".db", ".duckdb"))
            else db_path
        )
        self.db_path = self.gorgonzola_db_path

        parent_dir = os.path.dirname(self.gorgonzola_db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # If a legacy Kùzu directory or non-SQLite file exists at self.gorgonzola_db_path, clean it up
        if os.path.exists(self.gorgonzola_db_path):
            if os.path.isdir(self.gorgonzola_db_path):
                shutil.rmtree(self.gorgonzola_db_path, ignore_errors=True)
            elif os.path.isfile(self.gorgonzola_db_path) and os.path.getsize(self.gorgonzola_db_path) > 0:
                try:
                    with open(self.gorgonzola_db_path, "rb") as f:
                        header = f.read(16)
                    if not header.startswith(b"SQLite format 3"):
                        os.remove(self.gorgonzola_db_path)
                except OSError:
                    pass

    def _ensure_schema(self, conn: _SQLiteGraphConnection) -> None:
        if not self._schema_initialized:
            init_gorgonzola_schema(conn)
            self._schema_initialized = True

    def build_indexes(self) -> None:
        """Ensure SQLite indexes are built and analyzed."""
        with self._connection() as conn:
            conn.raw_conn.executescript(_SQLITE_SCHEMA)
            conn.raw_conn.execute("ANALYZE;")
            conn.raw_conn.commit()

    def __enter__(self):
        self._context_depth = getattr(self, "_context_depth", 0)
        if self._context_depth == 0:
            raw = sqlite3.connect(self.gorgonzola_db_path, timeout=30.0, check_same_thread=False)
            raw.execute("PRAGMA journal_mode=WAL;")
            raw.execute("PRAGMA synchronous=NORMAL;")
            raw.execute("PRAGMA temp_store=MEMORY;")
            raw.execute("PRAGMA busy_timeout=15000;")
            self._raw_conn = raw
            self._conn = _SQLiteGraphConnection(self, raw)
            self._in_context = True
            self._ensure_schema(self._conn)
        self._context_depth += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._context_depth = max(0, getattr(self, "_context_depth", 1) - 1)
        if self._context_depth == 0:
            self._in_context = False
            if self._raw_conn is not None:
                try:
                    if exc_type is None:
                        self._raw_conn.commit()
                except Exception:
                    pass
                try:
                    self._raw_conn.close()
                except Exception:
                    pass
            self._raw_conn = None
            self._conn = None
            self._db = None

    @contextlib.contextmanager
    def _connection(self):
        if self._in_context and self._conn is not None:
            yield self._conn
        else:
            with self:
                yield self._conn

    def close(self) -> None:
        """Close the active connection."""
        self._context_depth = 1
        self.__exit__(None, None, None)

    def _get_csv_dir(self) -> str:
        return os.path.dirname(self.gorgonzola_db_path) or "."

    def _get_node_label(self, node_id: str, conn=None) -> Optional[str]:
        with self._label_cache_lock:
            if node_id in self._label_cache:
                return self._label_cache[node_id]

        with self._connection() as c:
            row = c.raw_conn.execute(
                "SELECT kind FROM graph_nodes WHERE id = ? LIMIT 1", (node_id,)
            ).fetchone()
            if row and row[0]:
                lbl = row[0]
                with self._label_cache_lock:
                    self._label_cache[node_id] = lbl
                return lbl
            row_ident = c.raw_conn.execute(
                "SELECT 1 FROM graph_identifiers WHERE id = ? LIMIT 1", (node_id,)
            ).fetchone()
            if row_ident:
                with self._label_cache_lock:
                    self._label_cache[node_id] = "Identifier"
                return "Identifier"
        return None

    def insert_nodes_bulk(self, nodes) -> dict:
        """Insert nodes in bulk into SQLite graph_nodes and graph_identifiers."""
        with self._connection() as conn:
            return self._insert_nodes_bulk_conn(nodes, conn)

    def _insert_nodes_bulk_conn(self, nodes, conn: _SQLiteGraphConnection) -> dict:
        code_rows = []
        ident_rows = []
        res_map = {}

        for node_id, properties, label in nodes:
            if not node_id:
                continue
            norm_id = str(node_id).replace("\n", " ").replace("\r", "")
            props = dict(properties) if properties else {}
            res_map[norm_id] = label

            if label == "Identifier":
                tokens_val = props.get("tokens", [])
                if isinstance(tokens_val, list):
                    tokens_str = json.dumps(tokens_val)
                else:
                    tokens_str = str(tokens_val or "")
                emb_val = props.get("embedding")
                emb_str = json.dumps(emb_val) if isinstance(emb_val, list) else str(emb_val or "")
                ident_rows.append((
                    norm_id,
                    str(props.get("raw", "")),
                    tokens_str,
                    str(props.get("case_style", "")),
                    str(props.get("prefix", "")),
                    str(props.get("suffix", "")),
                    str(props.get("verb", "")),
                    str(props.get("entity", "")),
                    str(props.get("qualifier", "")),
                    1 if props.get("is_magic") else 0,
                    str(props.get("canonical_verb", "")),
                    str(props.get("canonical_entity", "")),
                    str(props.get("domain", "")),
                    str(props.get("intent", "")),
                    emb_str,
                ))
            else:
                kind = str(props.get("kind") or label or "CodeNode")
                name = str(props.get("name", ""))
                qname = str(props.get("qualified_name", ""))
                file_val = str(props.get("file") or props.get("filepath") or props.get("path") or "")
                filepath_val = str(props.get("filepath") or props.get("file") or props.get("path") or "")
                path_val = str(props.get("path") or filepath_val)
                ext_val = str(props.get("extension", ""))
                chash_val = str(props.get("content_hash", ""))
                lang_val = str(props.get("lang", ""))
                line_val = int(props.get("line") or props.get("start_line") or 0)
                end_line_val = int(props.get("end_line") or 0)
                mtime_val = float(props.get("mtime") or 0.0)
                comp_val = int(props.get("complexity") or 0)
                doc_val = str(props.get("docstring") or "")
                emb_val = props.get("embedding")
                emb_str = json.dumps(emb_val) if isinstance(emb_val, list) else str(emb_val or "")

                code_rows.append((
                    norm_id, kind, name, qname, file_val, filepath_val, path_val,
                    ext_val, chash_val, lang_val, line_val, end_line_val,
                    mtime_val, comp_val, doc_val, emb_str,
                ))

        raw = conn.raw_conn
        if code_rows:
            raw.executemany(
                """
                INSERT OR REPLACE INTO graph_nodes (
                    id, kind, name, qualified_name, file, filepath, path,
                    extension, content_hash, lang, line, end_line,
                    mtime, complexity, docstring, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                code_rows,
            )
        if ident_rows:
            raw.executemany(
                """
                INSERT OR REPLACE INTO graph_identifiers (
                    id, raw, tokens, case_style, prefix, suffix,
                    verb, entity, qualifier, is_magic, canonical_verb,
                    canonical_entity, domain, intent, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ident_rows,
            )
        raw.commit()

        with self._label_cache_lock:
            self._label_cache.update(res_map)
        return res_map

    def insert_edges_bulk(self, edges, id_map=None) -> None:
        """Insert edges in bulk into SQLite graph_edges."""
        if id_map:
            with self._label_cache_lock:
                self._label_cache.update(id_map)
        with self._connection() as conn:
            self._insert_edges_bulk_conn(edges, conn, id_map or {})

    def _insert_edges_bulk_conn(self, edges, conn: _SQLiteGraphConnection, label_map: dict) -> None:
        rows = []
        for item in edges:
            if len(item) == 4:
                src_id, dst_id, props, rel_type = item
            elif len(item) == 3:
                src_id, dst_id, rel_type = item
                props = {}
            else:
                continue
            if not src_id or not dst_id or not rel_type:
                continue
            props = props if isinstance(props, dict) else {}
            s_id = str(src_id).replace("\n", " ").replace("\r", "")
            d_id = str(dst_id).replace("\n", " ").replace("\r", "")
            r_type = str(rel_type).strip()

            rows.append((
                s_id,
                d_id,
                r_type,
                int(props.get("line") or 0),
                int(props.get("position") or 0),
                float(props.get("weight") or 0.0),
                float(props.get("score") or 0.0),
                float(props.get("confidence") or 0.0),
                1 if props.get("is_external") else 0,
                str(props.get("import_text") or ""),
                1 if props.get("is_read") else 0,
                1 if props.get("is_mutation") else 0,
                1 if props.get("is_taint") else 0,
            ))

        if rows:
            conn.raw_conn.executemany(
                """
                INSERT OR REPLACE INTO graph_edges (
                    src_id, dst_id, rel_type, line, position, weight, score,
                    confidence, is_external, import_text, is_read, is_mutation, is_taint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.raw_conn.commit()

    def _handle_copy_statement(self, copy_query: str, raw_conn: sqlite3.Connection) -> None:
        """Support legacy COPY <Table> FROM '<csv>' statements."""
        m = re.match(r"COPY\s+([A-Za-z0-9_]+)\s+FROM\s+'([^']+)'", copy_query, re.IGNORECASE)
        if not m:
            return
        table = m.group(1)
        csv_path = m.group(2)
        if not os.path.exists(csv_path):
            return
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = list(csv.reader(f))
        if not reader:
            return
        if table == "CodeNode":
            nodes = []
            for r in reader:
                if len(r) >= 10:
                    props = {
                        "kind": r[1], "name": r[2], "qualified_name": r[3],
                        "file": r[4], "line": r[5], "end_line": r[6],
                        "mtime": r[7], "complexity": r[8], "docstring": r[9],
                    }
                    nodes.append((r[0], props, r[1] or "CodeNode"))
            self._insert_nodes_bulk_conn(nodes, _SQLiteGraphConnection(self, raw_conn))
        elif table == "Identifier":
            nodes = []
            for r in reader:
                if len(r) >= 14:
                    props = {
                        "raw": r[1], "tokens": r[2], "case_style": r[3],
                        "prefix": r[4], "suffix": r[5], "verb": r[6],
                        "entity": r[7], "qualifier": r[8], "is_magic": r[9] == "true",
                        "canonical_verb": r[10], "canonical_entity": r[11],
                        "domain": r[12], "intent": r[13],
                    }
                    nodes.append((r[0], props, "Identifier"))
            self._insert_nodes_bulk_conn(nodes, _SQLiteGraphConnection(self, raw_conn))
        else:
            edges = []
            for r in reader:
                if len(r) < 2:
                    continue
                props: Dict[str, Any] = {}
                if table in ("SEMANTICALLY_RELATED", "FILE_CHANGES_WITH") and len(r) >= 3:
                    props["weight"] = float(r[2] or 0.0)
                elif table == "SIMILAR_TO" and len(r) >= 3:
                    props["score"] = float(r[2] or 0.0)
                elif table == "CALLS" and len(r) >= 3:
                    props["line"] = int(r[2] or 0)
                edges.append((r[0], r[1], props, table))
            self._insert_edges_bulk_conn(edges, _SQLiteGraphConnection(self, raw_conn), {})

    def purge_orphaned_identifiers(self, conn=None) -> None:
        """Purge Identifier nodes that have no incoming HAS_IDENTIFIER relationships."""
        with self._connection() as c:
            c.raw_conn.execute(
                """
                DELETE FROM graph_identifiers
                WHERE id NOT IN (
                    SELECT dst_id FROM graph_edges WHERE rel_type = 'HAS_IDENTIFIER'
                )
                """
            )
            c.raw_conn.commit()

    def _rewrite_cypher_query(self, query: str) -> str:
        target_nodes = [
            "Class", "Function", "Method", "Interface", "Module", "Type", "Symbol",
            "Macro", "Field", "Enum", "Property", "Struct", "Trait", "Variable",
            "Lambda", "ControlFlow", "Parameter", "ExternalType", "External",
            "Project", "Package", "Folder", "Route", "Resource",
        ]
        pattern = r"(\([a-zA-Z0-9_]*)\s*:\s*(" + "|".join(target_nodes) + r")\b(\s*\{?)"

        def repl(m):
            var_part = m.group(1)
            label = m.group(2)
            brace = m.group(3)
            if "{" in brace:
                return f"{var_part}:CodeNode {{kind: '{label}', "
            return f"{var_part}:CodeNode {{kind: '{label}'}}{brace}"

        query = re.sub(pattern, repl, query)
        query = re.sub(r"\blabel\(([a-zA-Z0-9_]+)\)", r"\1.kind", query)
        return query

    def query(self, query: str, parameters: dict = None) -> list:
        if parameters is None:
            parameters = {}
        with self._connection() as conn:
            return self._execute_cypher(query, parameters, conn.raw_conn)

    def query_batch(self, queries, parameters=None) -> None:
        if parameters is None:
            parameters = {}
        with self._connection() as conn:
            for q in queries:
                try:
                    self._execute_cypher(q, parameters, conn.raw_conn, commit=False)
                except Exception as e:
                    logger.debug("Batch query failed: %s - Error: %s", q.strip(), e)
            conn.raw_conn.commit()

    def pagerank(self, damping: float = 0.85, max_iter: int = 20) -> list:
        """Compute PageRank across the graph in pure Python."""
        try:
            with self._connection() as conn:
                edges = conn.raw_conn.execute(
                    "SELECT src_id, dst_id FROM graph_edges WHERE rel_type != 'HAS_IDENTIFIER'"
                ).fetchall()
                node_rows = conn.raw_conn.execute("SELECT id FROM graph_nodes").fetchall()

            nodes = {r[0] for r in node_rows if r[0]}
            out_adj: Dict[str, List[str]] = {}
            for u, v in edges:
                if u and v:
                    nodes.add(u)
                    nodes.add(v)
                    out_adj.setdefault(u, []).append(v)

            n = len(nodes)
            if n == 0:
                return []

            node_list = list(nodes)
            init_val = 1.0 / n
            scores = {nid: init_val for nid in node_list}
            base = (1.0 - damping) / n

            for _ in range(max_iter):
                next_scores = {nid: base for nid in node_list}
                dangling_sum = 0.0
                for u in node_list:
                    nbrs = out_adj.get(u)
                    if nbrs:
                        share = damping * scores[u] / len(nbrs)
                        for v in nbrs:
                            next_scores[v] += share
                    else:
                        dangling_sum += scores[u]
                if dangling_sum > 0:
                    dangling_share = damping * dangling_sum / n
                    for nid in node_list:
                        next_scores[nid] += dangling_share
                scores = next_scores

            return [{"node_id": nid, "score": float(score)} for nid, score in scores.items()]
        except Exception as e:
            logger.warning("PageRank calculation failed: %s", e)
            return []

    def compute_leiden_communities(self) -> dict:
        """Compute deterministic graph communities via label propagation / connected components."""
        try:
            with self._connection() as conn:
                edges = conn.raw_conn.execute(
                    "SELECT src_id, dst_id FROM graph_edges WHERE rel_type != 'HAS_IDENTIFIER'"
                ).fetchall()
                node_rows = conn.raw_conn.execute("SELECT id FROM graph_nodes").fetchall()

            adj: Dict[str, set] = {}
            for (nid,) in node_rows:
                if nid:
                    adj.setdefault(nid, set())
            for u, v in edges:
                if u and v:
                    adj.setdefault(u, set()).add(v)
                    adj.setdefault(v, set()).add(u)

            if not adj:
                return {}

            sorted_nodes = sorted(adj.keys())
            labels = {nid: i for i, nid in enumerate(sorted_nodes)}

            for _ in range(10):
                changed = False
                for nid in sorted_nodes:
                    nbrs = adj[nid]
                    if not nbrs:
                        continue
                    counts: Dict[int, int] = {}
                    for nb in nbrs:
                        lbl = labels[nb]
                        counts[lbl] = counts.get(lbl, 0) + 1
                    best_lbl = min(
                        counts.keys(),
                        key=lambda k: (-counts[k], k),
                    )
                    if best_lbl != labels[nid]:
                        labels[nid] = best_lbl
                        changed = True
                if not changed:
                    break

            # Compact community IDs to 0..C-1
            unique_lbls = sorted(set(labels.values()))
            remap = {old: idx for idx, old in enumerate(unique_lbls)}
            return {nid: remap[lbl] for nid, lbl in labels.items()}
        except Exception as e:
            logger.warning("Failed to compute communities: %s", e)
            return {}

    # ═══════════════════════════════════════════════════════════════════════════
    #  OpenCypher -> SQLite Execution Engine
    # ═══════════════════════════════════════════════════════════════════════════

    def _node_dict(self, row: sqlite3.Row, prefix: str = "") -> Dict[str, Any]:
        """Build a Kùzu-compatible node dict from a graph_nodes row."""
        def g(col):
            k = f"{prefix}{col}" if prefix else col
            return row[k] if k in row.keys() else None

        kind = g("kind") or "CodeNode"
        props = {
            "id": g("id") or "",
            "kind": kind,
            "name": g("name") or "",
            "qualified_name": g("qualified_name") or "",
            "file": g("file") or g("filepath") or "",
            "filepath": g("filepath") or g("file") or "",
            "path": g("path") or g("file") or "",
            "line": g("line") or 0,
            "end_line": g("end_line") or 0,
            "mtime": g("mtime") or 0.0,
            "complexity": g("complexity") or 0,
            "docstring": g("docstring") or "",
        }
        return {
            "properties": props,
            "labels": [kind],
            "label": kind,
            **props,
        }

    def _execute_cypher(
        self,
        query: str,
        parameters: Dict[str, Any],
        raw_conn: sqlite3.Connection,
        commit: bool = True,
    ) -> List[Dict[str, Any]]:
        q_orig = (query or "").strip().rstrip(";")
        if not q_orig:
            return []

        q_rewritten = self._rewrite_cypher_query(q_orig)
        q_norm = " ".join(q_rewritten.split())
        q_upper = q_norm.upper()

        # 1. CALL statements
        if q_upper.startswith("CALL "):
            if "LEIDEN" in q_upper:
                comms = self.compute_leiden_communities()
                col_id = "node_id" if "AS NODE_ID" in q_upper else "id"
                return [{col_id: k, "leiden_id": v} for k, v in comms.items()]
            if "PAGE_RANK" in q_upper:
                prs = self.pagerank()
                return [{"id": r["node_id"], "rank": r["score"]} for r in prs]
            return []

        # 2. MERGE (f:File {id: $id...}) ON CREATE SET ...
        if q_upper.startswith("MERGE (F:FILE") or q_upper.startswith("MERGE (F:CODENODE"):
            m_idx = re.search(r"\$id_(\d+)", q_norm)
            suffix = f"_{m_idx.group(1)}" if m_idx else ""
            fid = parameters.get(f"id{suffix}", "")
            if fid:
                name = parameters.get(f"name{suffix}", os.path.basename(fid))
                ext = parameters.get(f"ext{suffix}", os.path.splitext(fid)[1])
                chash = parameters.get(f"content_hash{suffix}", "")
                mtime = float(parameters.get(f"mtime{suffix}", 0.0))
                lang = parameters.get(f"lang{suffix}", "")
                raw_conn.execute(
                    """
                    INSERT INTO graph_nodes (id, kind, name, file, filepath, path, extension, content_hash, mtime, lang)
                    VALUES (?, 'File', ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        mtime=excluded.mtime,
                        lang=excluded.lang
                    """,
                    (fid, name, fid, fid, fid, ext, chash, mtime, lang),
                )
                if commit:
                    raw_conn.commit()
            return []

        # 3. Mutating queries (DELETE / DETACH DELETE / CREATE / MERGE edges)
        if any(kw in q_upper for kw in ("DELETE ", "CREATE (", "MERGE (")):
            self._execute_mutating_cypher(q_norm, parameters, raw_conn)
            if commit:
                raw_conn.commit()
            return []

        # 4. Read-only MATCH queries
        return self._execute_read_cypher(q_norm, q_orig, parameters, raw_conn)

    def _execute_mutating_cypher(
        self, q_norm: str, parameters: Dict[str, Any], raw_conn: sqlite3.Connection
    ) -> None:
        q_upper = q_norm.upper()

        # 3a. Delete all edges of a specific type: MATCH ()-[r:REL]->() DELETE r
        m_del_rel = re.match(
            r"MATCH\s+\(\)-\[\w*:([A-Za-z0-9_]+)\]->\(\)\s+DELETE\s+\w+",
            q_norm,
            re.IGNORECASE,
        )
        if m_del_rel:
            raw_conn.execute("DELETE FROM graph_edges WHERE rel_type = ?", (m_del_rel.group(1),))
            return

        # 3b. Delete self-loop edges: MATCH (m:CodeNode {kind: 'X'})-[r:RECURSES_TO]->(m) DELETE r
        m_self_del = re.search(
            r"MATCH\s+\(\w+:CodeNode\s*\{kind:\s*'([^']+)'\}\)-\[\w*:([A-Za-z0-9_]+)\]->\(\w+\)\s+DELETE",
            q_norm,
            re.IGNORECASE,
        )
        if m_self_del:
            kind, rel = m_self_del.group(1), m_self_del.group(2)
            raw_conn.execute(
                """
                DELETE FROM graph_edges
                WHERE rel_type = ? AND src_id = dst_id
                  AND src_id IN (SELECT id FROM graph_nodes WHERE kind = ?)
                """,
                (rel, kind),
            )
            return

        # 3c. Create self-loop RECURSES_TO: MATCH (m:CodeNode {kind: 'X'})-[:CALLS]->(m) CREATE (m)-[:RECURSES_TO]->(m)
        m_self_create = re.search(
            r"MATCH\s+\((\w+):CodeNode\s*\{kind:\s*'([^']+)'\}\)-\[:CALLS\]->\(\1\)\s+CREATE\s+\(\1\)-\[:RECURSES_TO\]->\(\1\)",
            q_norm,
            re.IGNORECASE,
        )
        if m_self_create:
            kind = m_self_create.group(2)
            raw_conn.execute(
                """
                INSERT OR IGNORE INTO graph_edges (src_id, dst_id, rel_type)
                SELECT e.src_id, e.dst_id, 'RECURSES_TO'
                FROM graph_edges e
                JOIN graph_nodes n ON n.id = e.src_id
                WHERE e.rel_type = 'CALLS' AND e.src_id = e.dst_id AND n.kind = ?
                """,
                (kind,),
            )
            return

        # 3d. Symbol resolution (both CREATE and MERGE ... DELETE r variants)
        if "S.KIND IN ['SYMBOL', 'EXTERNAL']" in q_upper:
            m_kinds = re.findall(r"kind:\s*'([^']+)'", q_norm)
            rel_match = re.search(r"-\[\w*:([A-Za-z0-9_]+)\]->\(s:CodeNode\)", q_norm)
            rel_type = rel_match.group(1) if rel_match else "CALLS"
            if len(m_kinds) >= 2:
                caller_kind, target_kind = m_kinds[0], m_kinds[1]
                raw_conn.execute(
                    """
                    INSERT OR IGNORE INTO graph_edges (src_id, dst_id, rel_type, line)
                    SELECT e.src_id, t.id, ?, e.line
                    FROM graph_edges e
                    JOIN graph_nodes caller ON caller.id = e.src_id
                    JOIN graph_nodes s ON s.id = e.dst_id
                    JOIN graph_nodes t ON (
                        s.name = t.name
                        OR (LENGTH(s.name) > LENGTH(t.name) AND SUBSTR(s.name, -LENGTH(t.name) - 1) = '.' || t.name)
                    )
                    WHERE e.rel_type = ?
                      AND caller.kind = ?
                      AND s.kind IN ('Symbol', 'External')
                      AND t.kind = ?
                    """,
                    (rel_type, rel_type, caller_kind, target_kind),
                )
                if "DELETE R" in q_upper:
                    raw_conn.execute(
                        """
                        DELETE FROM graph_edges
                        WHERE rel_type = ?
                          AND src_id IN (SELECT id FROM graph_nodes WHERE kind = ?)
                          AND dst_id IN (
                              SELECT s.id FROM graph_nodes s
                              JOIN graph_nodes t ON s.name = t.name
                              WHERE s.kind IN ('Symbol', 'External') AND t.kind = ?
                          )
                        """,
                        (rel_type, caller_kind, target_kind),
                    )
            return

        # 3e. Semantic fallback MERGE (caller)-[:LIKELY_CALLS {confidence: ...}]->(target)
        m_likely = re.search(
            r"MATCH\s+\(caller\s*\{id:\s*'([^']+)'\}\),\s*\(target\s*\{id:\s*'([^']+)'\}\)\s*MERGE\s+\(caller\)-\[:LIKELY_CALLS\s*\{confidence:\s*([0-9.eE+-]+)\}\]->\(target\)",
            q_norm,
            re.IGNORECASE,
        )
        if m_likely:
            raw_conn.execute(
                """
                INSERT OR REPLACE INTO graph_edges (src_id, dst_id, rel_type, confidence)
                VALUES (?, ?, 'LIKELY_CALLS', ?)
                """,
                (m_likely.group(1), m_likely.group(2), float(m_likely.group(3))),
            )
            return

        # 3f. Delete HAS_IDENTIFIER edges for $ids
        if "HAS_IDENTIFIER" in q_upper and "DELETE" in q_upper:
            ids = parameters.get("ids", [])
            if ids:
                ph = ",".join("?" for _ in ids)
                raw_conn.execute(
                    f"DELETE FROM graph_edges WHERE rel_type = 'HAS_IDENTIFIER' AND src_id IN ({ph})",
                    list(ids),
                )
            return

        # 3g. Purge orphaned identifiers
        if "IDENTIFIER" in q_upper and "DETACH DELETE" in q_upper and "NOT" in q_upper:
            self.purge_orphaned_identifiers()
            return

        # 3h. DETACH DELETE nodes by $ids
        if "DETACH DELETE" in q_upper and "IDS" in parameters:
            ids = list(parameters.get("ids", []))
            if ids:
                ph = ",".join("?" for _ in ids)
                raw_conn.execute(
                    f"DELETE FROM graph_edges WHERE src_id IN ({ph}) OR dst_id IN ({ph})",
                    ids + ids,
                )
                raw_conn.execute(f"DELETE FROM graph_nodes WHERE id IN ({ph})", ids)
            return

    def _execute_read_cypher(
        self,
        q_norm: str,
        q_orig: str,
        parameters: Dict[str, Any],
        raw_conn: sqlite3.Connection,
    ) -> List[Dict[str, Any]]:
        raw_conn.row_factory = sqlite3.Row
        q_upper = q_norm.upper()

        # Special Fast-Path 1: Orphan symbol edges check in verify_index_integrity
        if "NOT EXISTS" in q_upper and "CALLS" in q_upper and "COUNT(" in q_upper:
            row = raw_conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM graph_edges e
                JOIN graph_nodes b ON b.id = e.dst_id
                WHERE e.rel_type = 'CALLS'
                  AND b.kind IN ('Symbol', 'External')
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_nodes t
                      WHERE t.name = b.name AND t.kind NOT IN ('Symbol', 'External')
                  )
                """
            ).fetchone()
            return [{"cnt": row["cnt"] if row else 0}]

        # Special Fast-Path 2: Functional Purity — Lambda density
        if "CONTAINS_LAMBDA" in q_upper and "LAMBDA_COUNT" in q_upper:
            rows = raw_conn.execute(
                """
                WITH RECURSIVE contained(file_id, node_id, depth) AS (
                    SELECT e.src_id, e.dst_id, 1
                    FROM graph_edges e
                    JOIN graph_nodes f ON f.id = e.src_id
                    WHERE e.rel_type = 'CONTAINS' AND f.kind = 'File'
                    UNION ALL
                    SELECT c.file_id, e.dst_id, c.depth + 1
                    FROM contained c
                    JOIN graph_edges e ON e.src_id = c.node_id
                    WHERE e.rel_type = 'CONTAINS' AND c.depth < 3
                )
                SELECT f.name AS file, COUNT(l.id) AS lambda_count
                FROM contained c
                JOIN graph_nodes f ON f.id = c.file_id
                JOIN graph_edges el ON el.src_id = c.node_id AND el.rel_type = 'CONTAINS_LAMBDA'
                JOIN graph_nodes l ON l.id = el.dst_id AND l.kind = 'Lambda'
                GROUP BY f.id, f.name
                ORDER BY lambda_count DESC
                LIMIT 20
                """
            ).fetchall()
            return [{"file": r["file"], "lambda_count": r["lambda_count"]} for r in rows]

        # Special Fast-Path 3: Functional Purity — State mutation hotspots
        if "ACCESSES_STATE" in q_upper and "MUTATION_COUNT" in q_upper:
            rows = raw_conn.execute(
                """
                SELECT m.name AS function_name, COUNT(v.id) AS mutation_count
                FROM graph_edges a
                JOIN graph_nodes m ON m.id = a.src_id
                JOIN graph_nodes v ON v.id = a.dst_id
                WHERE a.rel_type = 'ACCESSES_STATE'
                  AND v.kind = 'Variable'
                  AND (a.is_mutation = 1 OR a.is_taint = 1)
                GROUP BY m.id, m.name
                ORDER BY mutation_count DESC
                LIMIT 20
                """
            ).fetchall()
            return [{"function_name": r["function_name"], "mutation_count": r["mutation_count"]} for r in rows]

        # Special Fast-Path 4: Functional Purity — Recursive loops
        if "RECURSES_TO" in q_upper and "RECURSIVE_FUNCTION" in q_upper:
            rows = raw_conn.execute(
                """
                SELECT m.name AS recursive_function, COALESCE(NULLIF(m.filepath, ''), m.file) AS filepath
                FROM graph_edges e
                JOIN graph_nodes m ON m.id = e.src_id
                WHERE e.rel_type = 'RECURSES_TO' AND e.src_id = e.dst_id
                LIMIT 50
                """
            ).fetchall()
            return [{"recursive_function": r["recursive_function"], "filepath": r["filepath"]} for r in rows]

        # Special Fast-Path 5: OOD Betweenness (2-hop CALLS bridges)
        if "BRIDGES" in q_upper and "A.ID <> B.ID" in q_upper:
            rows = raw_conn.execute(
                """
                SELECT e1.dst_id AS id, COUNT(*) AS bridges
                FROM graph_edges e1
                JOIN graph_edges e2 ON e1.dst_id = e2.src_id
                WHERE e1.rel_type = 'CALLS' AND e2.rel_type = 'CALLS'
                  AND e1.src_id <> e2.dst_id
                GROUP BY e1.dst_id
                """
            ).fetchall()
            return [{"id": r["id"], "bridges": r["bridges"]} for r in rows]

        # Special Fast-Path 6: trace_calls multi-hop queries
        if "CALLS|LIKELY_CALLS|DATA_FLOWS_TO" in q_norm:
            return self._execute_trace_calls_cypher(q_norm, parameters, raw_conn)

        # General Cypher Compiler for 0-hop, 1-hop, and variable-length (*min..max) MATCH queries
        return self._compile_and_run_cypher(q_norm, q_orig, parameters, raw_conn)

    def _execute_trace_calls_cypher(
        self, q_norm: str, parameters: Dict[str, Any], raw_conn: sqlite3.Connection
    ) -> List[Dict[str, Any]]:
        target_sym = parameters.get("target", "")
        m_varlen = re.search(r"\*(\d+)\.\.(\d+)", q_norm)
        hop_count = int(m_varlen.group(1)) + 1 if m_varlen else 1
        is_outbound = "WHERE (START.NAME" in q_norm.upper()

        if is_outbound:
            sql = """
            WITH RECURSIVE walk(curr_id, depth, edge_type, confidence) AS (
                SELECT e.dst_id, 1, e.rel_type, e.confidence
                FROM graph_nodes s
                JOIN graph_edges e ON e.src_id = s.id
                WHERE e.rel_type IN ('CALLS', 'LIKELY_CALLS', 'DATA_FLOWS_TO')
                  AND (s.name = ? OR s.name LIKE '%.' || ? OR INSTR(s.name, ?) > 0)
                UNION ALL
                SELECT e.dst_id, w.depth + 1, e.rel_type, e.confidence
                FROM walk w
                JOIN graph_edges e ON e.src_id = w.curr_id
                WHERE w.depth < ?
                  AND e.rel_type IN ('CALLS', 'LIKELY_CALLS', 'DATA_FLOWS_TO')
            )
            SELECT DISTINCT t.name AS name, t.id AS id, t.kind AS label, w.edge_type, w.confidence
            FROM walk w
            JOIN graph_nodes t ON t.id = w.curr_id
            WHERE w.depth = ?
            """
        else:
            sql = """
            WITH RECURSIVE walk(curr_id, depth, edge_type, confidence) AS (
                SELECT e.src_id, 1, e.rel_type, e.confidence
                FROM graph_nodes t
                JOIN graph_edges e ON e.dst_id = t.id
                WHERE e.rel_type IN ('CALLS', 'LIKELY_CALLS', 'DATA_FLOWS_TO')
                  AND (t.name = ? OR t.name LIKE '%.' || ? OR INSTR(t.name, ?) > 0)
                UNION ALL
                SELECT e.src_id, w.depth + 1, e.rel_type, e.confidence
                FROM walk w
                JOIN graph_edges e ON e.dst_id = w.curr_id
                WHERE w.depth < ?
                  AND e.rel_type IN ('CALLS', 'LIKELY_CALLS', 'DATA_FLOWS_TO')
            )
            SELECT DISTINCT s.name AS name, s.id AS id, s.kind AS label, w.edge_type, w.confidence
            FROM walk w
            JOIN graph_nodes s ON s.id = w.curr_id
            WHERE w.depth = ?
            """
        rows = raw_conn.execute(
            sql, (target_sym, target_sym, target_sym, hop_count, hop_count)
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "name": r["name"],
                "id": r["id"],
                "label": [r["label"]] if r["label"] else ["CodeNode"],
                "e": {
                    "_label": r["edge_type"],
                    "confidence": r["confidence"] if r["confidence"] else None,
                },
            })
        return out

    def _parse_node_pattern(self, pat: str) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """Parse '(var:Label {k: v, ...})' -> (var, label, props_dict)."""
        inner = pat.strip()[1:-1].strip()
        props: Dict[str, Any] = {}
        if "{" in inner and inner.endswith("}"):
            brace_idx = inner.index("{")
            props_str = inner[brace_idx + 1 : -1].strip()
            inner = inner[:brace_idx].strip()
            for part in re.split(r",\s*(?=[a-zA-Z0-9_]+\s*:)", props_str):
                if ":" in part:
                    k, v = part.split(":", 1)
                    props[k.strip()] = v.strip()
        if ":" in inner:
            var, lbl = inner.split(":", 1)
            return var.strip(), lbl.strip(), props
        return inner.strip(), None, props

    def _compile_and_run_cypher(
        self,
        q_norm: str,
        q_orig: str,
        parameters: Dict[str, Any],
        raw_conn: sqlite3.Connection,
    ) -> List[Dict[str, Any]]:
        # Split into MATCH, WHERE, RETURN, ORDER BY, LIMIT
        m_clauses = re.match(
            r"^MATCH\s+(?:[a-zA-Z0-9_]+\s*=\s*)?(.+?)"
            r"(?:\s+WHERE\s+(.+?))?"
            r"\s+RETURN\s+(.+?)"
            r"(?:\s+ORDER\s+BY\s+(.+?))?"
            r"(?:\s+LIMIT\s+(\d+))?$",
            q_norm,
            re.IGNORECASE,
        )
        if not m_clauses:
            return []

        match_part, where_part, return_part, order_part, limit_part = m_clauses.groups()

        # Check if 1-hop relationship or single node
        m_edge = re.match(
            r"^(\([^)]*\))\s*-\s*\[([^\]]*)\]\s*->\s*(\([^)]*\))$",
            match_part.strip(),
        )

        sql_params: List[Any] = []
        where_conds: List[str] = []
        alias_tables: Dict[str, str] = {}

        def resolve_val(raw_val: str) -> Tuple[str, List[Any]]:
            raw_val = raw_val.strip()
            if raw_val.startswith("$"):
                pname = raw_val[1:]
                val = parameters.get(pname)
                return "?", [val]
            if (raw_val.startswith("'") and raw_val.endswith("'")) or (
                raw_val.startswith('"') and raw_val.endswith('"')
            ):
                return "?", [raw_val[1:-1]]
            if raw_val.lower() in ("true", "false"):
                return "?", [1 if raw_val.lower() == "true" else 0]
            try:
                return "?", [int(raw_val)]
            except ValueError:
                try:
                    return "?", [float(raw_val)]
                except ValueError:
                    return raw_val, []

        def add_node_constraints(var: str, lbl: Optional[str], props: Dict[str, Any]):
            tbl = "graph_identifiers" if lbl == "Identifier" else "graph_nodes"
            alias_tables[var] = tbl
            if lbl and lbl not in ("CodeNode", "Identifier"):
                where_conds.append(f"{var}.kind = ?")
                sql_params.append(lbl)
            for k, v in props.items():
                if k == "kind" and v.strip("'\"") == "CodeNode":
                    continue
                ph, pvals = resolve_val(v)
                where_conds.append(f"{var}.{k} = {ph}")
                sql_params.extend(pvals)

        varlen_cte = ""
        if m_edge:
            left_pat, rel_pat, right_pat = m_edge.groups()
            u_var, u_lbl, u_props = self._parse_node_pattern(left_pat)
            v_var, v_lbl, v_props = self._parse_node_pattern(right_pat)
            if not u_var:
                u_var = "_u"
            if not v_var:
                v_var = "_v"

            add_node_constraints(u_var, u_lbl, u_props)
            add_node_constraints(v_var, v_lbl, v_props)

            rel_inner = rel_pat.strip()
            rel_var = "_e"
            rel_types: List[str] = []
            varlen_range: Optional[Tuple[int, int]] = None

            if "*" in rel_inner:
                before_star, after_star = rel_inner.split("*", 1)
                m_rng = re.match(r"(\d+)\.\.(\d+)", after_star.strip())
                if m_rng:
                    varlen_range = (int(m_rng.group(1)), int(m_rng.group(2)))
                else:
                    varlen_range = (1, 5)
                rel_inner = before_star.strip()

            if ":" in rel_inner:
                r_v, r_t = rel_inner.split(":", 1)
                if r_v.strip():
                    rel_var = r_v.strip()
                rel_types = [t.strip() for t in r_t.split("|") if t.strip()]
            elif rel_inner:
                rel_var = rel_inner

            u_tbl = alias_tables[u_var]
            v_tbl = alias_tables[v_var]

            if varlen_range:
                min_d, max_d = varlen_range
                rel_filter = ""
                if rel_types:
                    r_list = ",".join(f"'{rt}'" for rt in rel_types)
                    rel_filter = f"AND rel_type IN ({r_list})"
                varlen_cte = f"""
                WITH RECURSIVE _reach(src_id, dst_id, depth) AS (
                    SELECT src_id, dst_id, 1
                    FROM graph_edges
                    WHERE 1=1 {rel_filter}
                    UNION ALL
                    SELECT r.src_id, e.dst_id, r.depth + 1
                    FROM _reach r
                    JOIN graph_edges e ON e.src_id = r.dst_id
                    WHERE r.depth < {max_d} {rel_filter.replace('rel_type', 'e.rel_type')}
                )
                """
                from_sql = (
                    f"(SELECT DISTINCT src_id, dst_id FROM _reach WHERE depth >= {min_d}) {rel_var} "
                    f"JOIN {u_tbl} {u_var} ON {u_var}.id = {rel_var}.src_id "
                    f"JOIN {v_tbl} {v_var} ON {v_var}.id = {rel_var}.dst_id"
                )
            else:
                from_sql = (
                    f"graph_edges {rel_var} "
                    f"JOIN {u_tbl} {u_var} ON {u_var}.id = {rel_var}.src_id "
                    f"JOIN {v_tbl} {v_var} ON {v_var}.id = {rel_var}.dst_id"
                )
                if rel_types:
                    ph = ",".join("?" for _ in rel_types)
                    where_conds.append(f"{rel_var}.rel_type IN ({ph})")
                    sql_params.extend(rel_types)
        else:
            u_var, u_lbl, u_props = self._parse_node_pattern(match_part.strip())
            if not u_var:
                u_var = "_n"
            add_node_constraints(u_var, u_lbl, u_props)
            from_sql = f"{alias_tables[u_var]} {u_var}"

        # Translate WHERE clause
        if where_part:
            w_sql, w_params = self._translate_where(where_part, parameters)
            if w_sql:
                where_conds.append(f"({w_sql})")
                sql_params.extend(w_params)

        # Translate RETURN clause
        select_exprs: List[str] = []
        return_items_meta: List[Tuple[str, str, Optional[str]]] = []  # (out_key, mode, var_name)
        group_by_cols: List[str] = []
        has_agg = False

        for item in [x.strip() for x in return_part.split(",") if x.strip()]:
            m_as = re.match(r"^(.+?)\s+AS\s+([a-zA-Z0-9_]+)$", item, re.IGNORECASE)
            if m_as:
                expr, alias = m_as.group(1).strip(), m_as.group(2).strip()
            else:
                expr, alias = item, item

            expr_upper = expr.upper()
            if expr_upper.startswith("COUNT("):
                has_agg = True
                inner_c = expr[6:-1].strip()
                if inner_c == "*" or not inner_c:
                    select_exprs.append(f"COUNT(*) AS \"{alias}\"")
                elif "." in inner_c:
                    select_exprs.append(f"COUNT({inner_c}) AS \"{alias}\"")
                elif inner_c in alias_tables:
                    select_exprs.append(f"COUNT({inner_c}.id) AS \"{alias}\"")
                else:
                    select_exprs.append(f"COUNT(*) AS \"{alias}\"")
                return_items_meta.append((alias, "scalar", None))
            elif re.match(r"^LABELS\(([a-zA-Z0-9_]+)\)$", expr, re.IGNORECASE):
                vname = re.match(r"^LABELS\(([a-zA-Z0-9_]+)\)$", expr, re.IGNORECASE).group(1)
                select_exprs.append(f"{vname}.kind AS \"{alias}\"")
                group_by_cols.append(f"{vname}.kind")
                return_items_meta.append((alias, "labels_list", vname))
            elif re.match(r"^LABEL\(([a-zA-Z0-9_]+)\)$", expr, re.IGNORECASE):
                vname = re.match(r"^LABEL\(([a-zA-Z0-9_]+)\)$", expr, re.IGNORECASE).group(1)
                if vname in alias_tables:
                    select_exprs.append(f"{vname}.kind AS \"{alias}\"")
                    group_by_cols.append(f"{vname}.kind")
                else:
                    select_exprs.append(f"{vname}.rel_type AS \"{alias}\"")
                    group_by_cols.append(f"{vname}.rel_type")
                return_items_meta.append((alias, "scalar", None))
            elif expr in alias_tables:
                # Returning a whole node variable e.g. RETURN caller
                cols = [
                    "id", "kind", "name", "qualified_name", "file", "filepath",
                    "path", "line", "end_line", "mtime", "complexity", "docstring",
                ]
                for c in cols:
                    select_exprs.append(f"{expr}.{c} AS \"{alias}__{c}\"")
                group_by_cols.append(f"{expr}.id")
                return_items_meta.append((alias, "node", expr))
            elif expr.endswith(".*"):
                vname = expr[:-2].strip()
                select_exprs.append(f"{vname}.line AS \"{alias}__line\"")
                return_items_meta.append((alias, "scalar", None))
            else:
                select_exprs.append(f"{expr} AS \"{alias}\"")
                group_by_cols.append(expr)
                return_items_meta.append((alias, "scalar", None))

        where_sql = f"WHERE {' AND '.join(where_conds)}" if where_conds else ""
        group_sql = ""
        if has_agg and group_by_cols:
            group_sql = f"GROUP BY {', '.join(group_by_cols)}"

        order_sql = f"ORDER BY {order_part}" if order_part else ""
        limit_sql = f"LIMIT {int(limit_part)}" if limit_part else ""

        full_sql = f"{varlen_cte} SELECT {', '.join(select_exprs)} FROM {from_sql} {where_sql} {group_sql} {order_sql} {limit_sql}"
        rows = raw_conn.execute(full_sql, sql_params).fetchall()

        results: List[Dict[str, Any]] = []
        for r in rows:
            out_row: Dict[str, Any] = {}
            for out_key, mode, var_name in return_items_meta:
                if mode == "node":
                    out_row[out_key] = self._node_dict(r, prefix=f"{out_key}__")
                elif mode == "labels_list":
                    val = r[out_key]
                    out_row[out_key] = [val] if val else ["CodeNode"]
                else:
                    out_row[out_key] = r[out_key] if out_key in r.keys() else None
            results.append(out_row)
        return results

    def _translate_where(
        self, where_str: str, parameters: Dict[str, Any]
    ) -> Tuple[str, List[Any]]:
        """Translate Cypher WHERE predicates to SQLite SQL + bound parameters."""
        params: List[Any] = []
        s = where_str.strip()

        # 1. Fuzzy name match pattern from GraphAPI._name_match_clause:
        #    (var.name = $target OR ends_with(var.name, '.' + $target) OR var.name CONTAINS $target)
        def repl_fuzzy(m):
            var = m.group(1)
            pname = m.group(2)
            val = str(parameters.get(pname, ""))
            params.extend([val, f"%.{val}", val])
            return f"({var}.name = ? OR {var}.name LIKE ? OR INSTR({var}.name, ?) > 0)"

        s = re.sub(
            r"\(\s*([a-zA-Z0-9_]+)\.name\s*=\s*\$([a-zA-Z0-9_]+)\s+OR\s+ends_with\(\1\.name,\s*'\.'\s*\+\s*\$\2\)\s+OR\s+\1\.name\s+CONTAINS\s+\$\2\s*\)",
            repl_fuzzy,
            s,
            flags=re.IGNORECASE,
        )

        # 2. ends_with(a, '.' + b)
        s = re.sub(
            r"ends_with\(([^,]+),\s*'\.'\s*\+\s*([^)]+)\)",
            r"(\1 LIKE '%.' || \2)",
            s,
            flags=re.IGNORECASE,
        )

        # 3. X CONTAINS $param or 'lit'
        def repl_contains(m):
            lhs, rhs = m.group(1).strip(), m.group(2).strip()
            if rhs.startswith("$"):
                params.append(str(parameters.get(rhs[1:], "")))
                return f"INSTR({lhs}, ?) > 0"
            if rhs.startswith(("'", '"')):
                params.append(rhs[1:-1])
                return f"INSTR({lhs}, ?) > 0"
            return f"INSTR({lhs}, {rhs}) > 0"

        s = re.sub(
            r"([a-zA-Z0-9_.]+)\s+CONTAINS\s+(\$[a-zA-Z0-9_]+|'[^']*'|\"[^\"]*\")",
            repl_contains,
            s,
            flags=re.IGNORECASE,
        )

        # 4. X IN $list_param
        def repl_in_param(m):
            lhs, pname = m.group(1).strip(), m.group(2).strip()
            items = list(parameters.get(pname, []))
            if not items:
                return "0=1"
            ph = ",".join("?" for _ in items)
            params.extend(items)
            return f"{lhs} IN ({ph})"

        s = re.sub(
            r"([a-zA-Z0-9_.]+)\s+IN\s+\$([a-zA-Z0-9_]+)",
            repl_in_param,
            s,
            flags=re.IGNORECASE,
        )

        # 5. X IN ['a', 'b', ...] -> X IN ('a', 'b', ...)
        s = re.sub(
            r"\bIN\s+\[([^\]]+)\]",
            r"IN (\1)",
            s,
            flags=re.IGNORECASE,
        )

        # 6. Remaining $param references
        def repl_param(m):
            pname = m.group(1)
            params.append(parameters.get(pname))
            return "?"

        s = re.sub(r"\$([a-zA-Z0-9_]+)", repl_param, s)

        # 7. Boolean literals
        s = re.sub(r"\btrue\b", "1", s, flags=re.IGNORECASE)
        s = re.sub(r"\bfalse\b", "0", s, flags=re.IGNORECASE)

        return s, params
