import csv
import logging
import os
import threading

logger = logging.getLogger(__name__)


# Column orders for each node table (matching CREATE statements, used for CSV COPY)
_NODE_COLUMNS = {
    "CodeNode": ["id", "kind", "name", "qualified_name", "file", "line", "end_line", "mtime", "complexity", "docstring", "embedding"],
    "Identifier": ["id", "raw", "tokens", "case_style", "prefix", "suffix", "verb", "entity", "qualifier", "is_magic", "canonical_verb", "canonical_entity", "domain", "intent", "embedding"]
}

# Columns that should default to 0 instead of empty string when missing
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
    "CREATE REL TABLE FILE_CHANGES_WITH (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE RAISES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE TESTS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE HTTP_CALLS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE READS (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE WRITES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE HAS_PARAMETER (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE USES (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE CONTAINS_LAMBDA (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE ACCESSES_STATE (FROM CodeNode TO CodeNode)",
    "CREATE REL TABLE RECURSES_TO (FROM CodeNode TO CodeNode)"
]

def init_gorgonzola_schema(conn):
    # Check if schema is already initialized
    try:
        tables_res = conn.execute("CALL show_tables() RETURN *;")
        existing_tables = set()
        while tables_res.has_next():
            existing_tables.add(tables_res.get_next()[1])
        tables_res.close()
    except Exception:
        existing_tables = set()

    if "CodeNode" not in existing_tables:
        queries = [
            # Create node tables
            "CREATE NODE TABLE CodeNode (id STRING, kind STRING, name STRING, qualified_name STRING, file STRING, line INT64, end_line INT64, mtime DOUBLE, complexity INT64, docstring STRING, embedding DOUBLE[384], PRIMARY KEY (id))",
            "CREATE NODE TABLE Identifier (id STRING, raw STRING, tokens STRING[], case_style STRING, prefix STRING, suffix STRING, verb STRING, entity STRING, qualifier STRING, is_magic BOOLEAN, canonical_verb STRING, canonical_entity STRING, domain STRING, intent STRING, embedding DOUBLE[384], PRIMARY KEY (id))",

            # Create relationship tables
        ] + _RELATIONSHIP_SCHEMA
        for q in queries:
            r = conn.execute(q)
            r.close()

class GorgonzolaGraph:
    def __init__(self, db_path: str):
        self._label_cache = {}
        self._label_cache_lock = threading.Lock()
        self._db = None
        self._conn = None
        self._in_context = False
        self._schema_initialized = False
        # Normalize path
        from src.mcp_server.index_db import get_graph_path_for_repo
        self.gorgonzola_db_path = get_graph_path_for_repo(db_path) if db_path.endswith('.duckdb') else db_path

        parent_dir = os.path.dirname(self.gorgonzola_db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        import gorgonzola
        self.gorgonzola = gorgonzola

    def _ensure_schema(self, conn):
        if not self._schema_initialized:
            init_gorgonzola_schema(conn)
            self._schema_initialized = True

    def build_indexes(self):
        """Build FTS and Vector indexes after a bulk load."""
        queries = [
            "CALL CREATE_FTS_INDEX('CodeNode', 'id', ['name', 'docstring'], stemmer='porter')",
            "CALL CREATE_FTS_INDEX('Identifier', 'id', ['raw', 'canonical_verb', 'canonical_entity'])",
            "CALL CREATE_VECTOR_INDEX('Identifier', 'id', 'embedding', metric='cosine')",
            "CALL CREATE_VECTOR_INDEX('CodeNode', 'id', 'embedding', metric='cosine')"
        ]
        if self._conn:
            for q in queries:
                try:
                    r = self._conn.execute(q)
                    r.close()
                except Exception as e:
                    logger.warning(f"Failed to build index with query {q}: {e}")
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            ext_path = os.path.join(base_dir, "modules/gorgonzola/modules/extension/algo/build/libalgo.gorgonzola_extension")
            conn.execute(f"LOAD EXTENSION '{ext_path}';")
        except Exception as e:
            logger.warning(f"Failed to load Leiden extension: {e}")

    def __enter__(self):
        self._db_ctx = self.gorgonzola.Database(self.gorgonzola_db_path)
        self._db = self._db_ctx.__enter__()
        self._conn_ctx = self.gorgonzola.Connection(self._db)
        self._conn = self._conn_ctx.__enter__()
        self._in_context = True
        self._ensure_schema(self._conn)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._in_context = False
        if hasattr(self, '_conn_ctx') and self._conn_ctx:
            try:
                self._conn_ctx.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
            self._conn_ctx = None
        if hasattr(self, '_db_ctx') and self._db_ctx:
            try:
                self._db_ctx.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
            self._db_ctx = None
        self._conn = None
        self._db = None

    def _get_csv_dir(self) -> str:
        """Return directory for temporary CSV files (same parent as DB)."""
        return os.path.dirname(self.gorgonzola_db_path) or "."

    def _get_node_label(self, node_id: str, conn) -> str:
        with self._label_cache_lock:
            if node_id in self._label_cache:
                return self._label_cache[node_id]
        res = conn.execute("MATCH (n:CodeNode {id: $id}) RETURN n.kind", {"id": node_id})
        lbl = None
        if res.has_next():
            lbl = res.get_next()[0]
        res.close()
        if lbl:
            with self._label_cache_lock:
                self._label_cache[node_id] = lbl
            return lbl
        return None

    def _write_and_copy_csv(self, conn, csv_path, rows, copy_query):
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                writer.writerows(rows)
            r = conn.execute(copy_query)
            r.close()
        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)

    def _rewrite_cypher_query(self, query: str) -> str:
        import re
        target_nodes = ["Project", "Package", "Folder", "File", "Module", "Class", "Function", "Method", "Interface", "Enum", "Type", "Route", "Resource", "Symbol"]
        pattern = r'(\([a-zA-Z0-9_]*)\s*:\s*(' + '|'.join(target_nodes) + r')\b(\s*\{?)'
        def repl(m):
            var_part = m.group(1)
            label = m.group(2)
            brace = m.group(3)
            if '{' in brace:
                return f"{var_part}:CodeNode {{kind: '{label}', "
            else:
                return f"{var_part}:CodeNode {{kind: '{label}'}}{brace}"
        query = re.sub(pattern, repl, query)
        
        # Rewrite label(x) to x.kind to mask the underlying CodeNode table
        query = re.sub(r'\blabel\(([a-zA-Z0-9_]+)\)', r'\1.kind', query)
        return query

    def query(self, query: str, parameters: dict = None) -> list:
        if parameters is None:
            parameters = {}
            
        query = self._rewrite_cypher_query(query)

        if self._in_context:
            return self._query_conn(query, parameters, self._conn)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    return self._query_conn(query, parameters, conn)

    def compute_leiden_communities(self) -> dict:
        """
        Runs the Leiden algorithm on the graph and returns the communities.
        It projects the CALLS relationships between functions, methods, and classes.
        Returns a dictionary mapping node id to its community leiden_id.
        """
        with self:
            try:
                self._conn.execute("CALL DROP_PROJECTED_GRAPH('call_graph')")
            except Exception:
                pass
            
            try:
                self._conn.execute("CALL PROJECT_GRAPH('call_graph', ['CodeNode'], ['CALLS'])")
                res = self._conn.execute("CALL leiden('call_graph') RETURN node.id AS id, leiden_id")
                communities = {}
                while res.has_next():
                    row = res.get_next()
                    node_id = row[0]
                    leiden_id = row[1]
                    communities[node_id] = leiden_id
                res.close()
                return communities
            except Exception as e:
                logger.error("Failed to compute Leiden communities: %s", e)
                return {}

    def _query_conn(self, query: str, parameters: dict, conn) -> list:
        res = conn.execute(query, parameters)
        res.rows_as_dict(True)

        results = []
        while res.has_next():
            row = res.get_next()
            formatted_row = self._format_row(row)
            results.append(formatted_row)
        res.close()
        del res
        return results

    def _format_row(self, row):
        formatted = {}
        for k, v in row.items():
            formatted[k] = self._format_value(v)
        return formatted

    def _format_value(self, v):
        if isinstance(v, dict):
            if ("_label" in v and "_id" in v) or ("_LABEL" in v and "_ID" in v):
                label_key = "_label" if "_label" in v else "_LABEL"
                properties = {key: val for key, val in v.items() if not key.startswith("_") and val is not None}
                return {
                    "properties": properties,
                    "labels": [v[label_key]],
                    "label": v[label_key]
                }
            elif ("_src" in v and "_dst" in v) or ("_SRC" in v and "_DST" in v):
                label_key = "_label" if "_label" in v else "_LABEL"
                properties = {key: val for key, val in v.items() if not key.startswith("_") and val is not None}
                return {
                    "properties": properties,
                    "type": v.get(label_key, "Unknown")
                }
            else:
                return {key: self._format_value(val) for key, val in v.items()}
        elif isinstance(v, list):
            return [self._format_value(item) for item in v]
        else:
            return v

    def insert_nodes_bulk(self, nodes) -> dict:
        """Insert nodes using CSV COPY FROM for performance.
        
        Args:
            nodes: list of (node_id, properties_dict, label) tuples
            
        Returns:
            dict mapping node_id -> label
        """
        if self._in_context:
            return self._insert_nodes_bulk_conn(nodes, self._conn)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    return self._insert_nodes_bulk_conn(nodes, conn)

    def _insert_nodes_bulk_conn(self, nodes, conn) -> dict:
        # Group nodes by label
        groups = {}
        for node_id, properties, label in nodes:
            if label == "Identifier":
                groups.setdefault("Identifier", []).append((node_id, properties))
            else:
                if "type" in properties:
                    properties["cf_type"] = properties.get("type", "")
                properties["kind"] = label
                groups.setdefault("CodeNode", []).append((node_id, properties))

        csv_dir = self._get_csv_dir()

        for label, group in groups.items():
            columns = _NODE_COLUMNS.get(label)
            if columns is None:
                # Fallback: skip unknown labels
                logger.warning(f"[WARNING] Unknown node label: {label}, skipping")
                continue

            # Query existing IDs to avoid duplicate primary key errors during incremental indexing
            ids_to_check = []
            for node_id, _ in group:
                with self._label_cache_lock:
                    if node_id not in self._label_cache:
                        ids_to_check.append(node_id)
            
            existing_ids = set()
            chunk_size = 500
            for i in range(0, len(ids_to_check), chunk_size):
                chunk = ids_to_check[i:i+chunk_size]
                try:
                    res = conn.execute(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", {"ids": chunk})
                    while res.has_next():
                        existing_ids.add(res.get_next()[0])
                    res.close()
                except Exception as e:
                    logger.warning(f"Failed to check existing ids for {label}: {e}")

            # Filter group to only new nodes (not in DB, not in cache)
            filtered_group = []
            for nid, props in group:
                with self._label_cache_lock:
                    if nid in self._label_cache:
                        continue
                if nid in existing_ids:
                    continue
                filtered_group.append((nid, props))
                
            if not filtered_group:
                continue

            csv_path = os.path.join(csv_dir, f"_bulk_nodes_{label}.csv")
            rows = []
            for node_id, properties in filtered_group:
                row = []
                for col in columns:
                    if col == "id":
                        row.append(str(node_id).replace('\n', ' ').replace('\r', ''))
                    elif col in properties and properties[col] is not None:
                        val = properties[col]
                        if col in _NUMERIC_COLUMNS:
                            row.append(int(val) if col != "mtime" else float(val))
                        elif isinstance(val, list):
                            # Gorgonzola expects [a,b,c] for array in CSV
                            row.append("[" + ",".join(str(v).replace('\n', ' ').replace('\r', '') for v in val) + "]")
                        else:
                            row.append(str(val).replace('\n', ' ').replace('\r', ''))
                    else:
                        # Default: 0 for numeric columns, empty string for strings
                        if col in _NUMERIC_COLUMNS:
                            row.append(0)
                        else:
                            row.append("")
                rows.append(row)

            copy_query = f"COPY {label} FROM '{csv_path}' (HEADER=false, PARALLEL=false, ESCAPE='\"', QUOTE='\"', DELIM=',', AUTO_DETECT=false)"
            self._write_and_copy_csv(conn, csv_path, rows, copy_query)

        res_map = {node_id: label for node_id, _, label in nodes}
        with self._label_cache_lock:
            self._label_cache.update(res_map)
        return res_map

    def insert_edges_bulk(self, edges, id_map=None):
        """Insert edges using CSV COPY FROM for performance.
        
        Args:
            edges: list of (src_id, dst_id, properties_dict, rel_type) tuples
            id_map: dict mapping node_id -> label for label lookups
        """
        label_map = id_map or {}
        if label_map:
            with self._label_cache_lock:
                self._label_cache.update(label_map)
        if self._in_context:
            self._insert_edges_bulk_conn(edges, self._conn, label_map)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    self._insert_edges_bulk_conn(edges, conn, label_map)

    def _insert_edges_bulk_conn(self, edges, conn, label_map):
        groups = {}
        for src_id, dst_id, props, rel_type in edges:
            src_label = "CodeNode"
            dst_label = "CodeNode"
            if rel_type == "HAS_IDENTIFIER":
                dst_label = "Identifier"

            if not src_label or not dst_label:
                continue

            key = (rel_type, src_label, dst_label)
            groups.setdefault(key, []).append((src_id, dst_id, props))

        csv_dir = self._get_csv_dir()

        for (rel_type, src_label, dst_label), items in groups.items():
            csv_path = os.path.join(csv_dir, f"_bulk_edges_{rel_type}_{src_label}_{dst_label}.csv")
            try:
                rows = []
                for src_id, dst_id, props in items:
                    row = [
                        str(src_id).replace('\n', ' ').replace('\r', ''),
                        str(dst_id).replace('\n', ' ').replace('\r', '')
                    ]
                    if rel_type == "ACCESSES_STATE":
                        props = props or {}
                        row.append(str(props.get("is_read", False)).lower())
                        row.append(str(props.get("is_mutation", False)).lower())
                        row.append(str(props.get("is_taint", False)).lower())
                    rows.append(row)

                copy_query = f"COPY {rel_type} FROM '{csv_path}' (HEADER=false, PARALLEL=false, FROM='{src_label}', TO='{dst_label}', ESCAPE='\"', QUOTE='\"', DELIM=',', AUTO_DETECT=false)"
                self._write_and_copy_csv(conn, csv_path, rows, copy_query)
            except Exception as e:
                logger.error("Failed to COPY edges %s-[:%s]->%s: %s", src_label, rel_type, dst_label, e)

    def query_batch(self, queries, parameters=None):
        """Execute multiple queries within a single connection."""
        if parameters is None:
            parameters = {}
        if self._in_context:
            self._query_batch_conn(queries, parameters, self._conn)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    self._query_batch_conn(queries, parameters, conn)

    def _query_batch_conn(self, queries, parameters, conn):
        for q in queries:
            try:
                r = conn.execute(q, parameters)
                r.close()
            except Exception as e:
                logger.warning(f"Batch query failed: {q.strip()} - Error: {e}")

    def pagerank(self) -> list:
        try:
            results = []
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)

                    # Project graph
                    try:
                        conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                    except Exception:
                        pass

                    conn.execute("""
                        CALL PROJECT_GRAPH('CodeGraph', 
                            ['File', 'Class', 'Method', 'Function', 'Interface', 'Symbol', 'Module', 'ControlFlow', 'Lambda', 'Variable', 'Folder', 'TestFile', 'Route', 'EnvVar', 'Type'],
                            ['DEPENDS_ON', 'CONTAINS', 'DEFINES', 'INHERITS', 'IMPLEMENTS', 'CALLS', 'FILE_CHANGES_WITH', 'RAISES', 'TESTS', 'HTTP_CALLS', 'IMPORTS', 'READS', 'WRITES', 'RETURNS', 'HAS_PARAMETER', 'USES']
                        );
                    """)

                    res = conn.execute("CALL page_rank('CodeGraph') RETURN node.id AS id, rank;")
                    while res.has_next():
                        row = res.get_next()
                        results.append({"node_id": row[0], "score": row[1]})
                    res.close()

                    try:
                        conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                    except Exception:
                        pass

            return results
        except Exception as e:
            logger.warning("PageRank calculation failed: %s", e)
            return []
