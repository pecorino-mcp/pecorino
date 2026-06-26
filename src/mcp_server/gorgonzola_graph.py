import csv
import os
import sys

# Column orders for each node table (matching CREATE statements, used for CSV COPY)
_NODE_COLUMNS = {
    "File": ["id", "name", "path", "extension", "content_hash", "mtime", "lang"],
    "Class": ["id", "name", "filepath", "start_line", "end_line"],
    "Method": ["id", "name", "complexity", "filepath", "start_line", "end_line"],
    "Function": ["id", "name", "complexity", "filepath", "start_line", "end_line"],
    "Interface": ["id", "name", "filepath", "start_line", "end_line"],
    "Symbol": ["id", "name"],
    "Module": ["id", "name"],
}

# Columns that should default to 0 instead of empty string when missing
_NUMERIC_COLUMNS = {"complexity", "start_line", "end_line", "mtime"}

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

    if "File" not in existing_tables:
        queries = [
            # Create node tables
            "CREATE NODE TABLE File (id STRING, name STRING, path STRING, extension STRING, content_hash STRING, mtime DOUBLE, lang STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Class (id STRING, name STRING, filepath STRING, start_line INT64, end_line INT64, PRIMARY KEY (id))",
            "CREATE NODE TABLE Method (id STRING, name STRING, complexity INT64, filepath STRING, start_line INT64, end_line INT64, PRIMARY KEY (id))",
            "CREATE NODE TABLE Function (id STRING, name STRING, complexity INT64, filepath STRING, start_line INT64, end_line INT64, PRIMARY KEY (id))",
            "CREATE NODE TABLE Interface (id STRING, name STRING, filepath STRING, start_line INT64, end_line INT64, PRIMARY KEY (id))",
            "CREATE NODE TABLE Symbol (id STRING, name STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Module (id STRING, name STRING, PRIMARY KEY (id))",

            # Create relationship tables
            "CREATE REL TABLE CONTAINS (FROM File TO Class, FROM Class TO Method, FROM Class TO Function, FROM Class TO Class, FROM File TO Interface, FROM Interface TO Method, FROM File TO Function, FROM Function TO Method, FROM Function TO Function, FROM Function TO Class)",
            "CREATE REL TABLE EXTENDS (FROM Class TO Symbol, FROM Class TO Class)",
            "CREATE REL TABLE IMPLEMENTS (FROM Class TO Symbol, FROM Class TO Interface)",
            "CREATE REL TABLE CALLS (FROM Method TO Symbol, FROM Method TO Method, FROM Method TO Function, FROM Function TO Symbol, FROM Function TO Method, FROM Function TO Function)",
            "CREATE REL TABLE DEPENDS_ON (FROM File TO File, FROM File TO Module)"
        ]
        for q in queries:
            r = conn.execute(q)
            r.close()

class GorgonzolaGraph:
    def __init__(self, db_path: str):
        self._label_cache = {}
        self._db = None
        self._conn = None
        self._in_context = False
        self._schema_initialized = False
        # Normalize path
        if db_path.endswith(".duckdb"):
            self.gorgonzola_db_path = db_path[:-7] + "_gorgonzola"
        else:
            self.gorgonzola_db_path = db_path
        
        parent_dir = os.path.dirname(self.gorgonzola_db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        import gorgonzola
        self.gorgonzola = gorgonzola

    def _ensure_schema(self, conn):
        if not self._schema_initialized:
            init_gorgonzola_schema(conn)
            self._schema_initialized = True

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
        if node_id in self._label_cache:
            return self._label_cache[node_id]
        tables = ["File", "Class", "Method", "Function", "Interface", "Symbol", "Module"]
        for t in tables:
            res = conn.execute(f"MATCH (n:{t} {{id: $id}}) RETURN label(n) AS lbl", {"id": node_id})
            lbl = None
            if res.has_next():
                lbl = res.get_next()[0]
            res.close()
            if lbl:
                self._label_cache[node_id] = lbl
                return lbl
        return None

    def query(self, query: str, parameters: dict = None) -> list:
        if parameters is None:
            parameters = {}
        
        if self._in_context:
            return self._query_conn(query, parameters, self._conn)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    return self._query_conn(query, parameters, conn)

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
            groups.setdefault(label, []).append((node_id, properties))

        csv_dir = self._get_csv_dir()

        for label, group in groups.items():
            columns = _NODE_COLUMNS.get(label)
            if columns is None:
                # Fallback: skip unknown labels
                print(f"[WARNING] Unknown node label: {label}, skipping", file=sys.stderr)
                continue

            csv_path = os.path.join(csv_dir, f"_bulk_nodes_{label}.csv")
            try:
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    for node_id, properties in group:
                        row = []
                        for col in columns:
                            if col == "id":
                                row.append(str(node_id).replace('\n', ' ').replace('\r', ''))
                            elif col in properties and properties[col] is not None:
                                val = properties[col]
                                if col in _NUMERIC_COLUMNS:
                                    row.append(int(val) if col != "mtime" else float(val))
                                else:
                                    row.append(str(val).replace('\n', ' ').replace('\r', ''))
                            else:
                                # Default: 0 for numeric columns, empty string for strings
                                if col in _NUMERIC_COLUMNS:
                                    row.append(0)
                                else:
                                    row.append("")
                        writer.writerow(row)

                r = conn.execute(f"COPY {label} FROM '{csv_path}' (HEADER=false, PARALLEL=false, ESCAPE='\"', QUOTE='\"', DELIM=',', AUTO_DETECT=false)")
                r.close()
            finally:
                if os.path.exists(csv_path):
                    os.remove(csv_path)

        res_map = {node_id: label for node_id, _, label in nodes}
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
            self._label_cache.update(label_map)
        if self._in_context:
            self._insert_edges_bulk_conn(edges, self._conn, label_map)
        else:
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    self._insert_edges_bulk_conn(edges, conn, label_map)

    def _insert_edges_bulk_conn(self, edges, conn, label_map):
        # Group edges by (rel_type, src_label, dst_label)
        groups = {}
        for src_id, dst_id, properties, rel_type in edges:
            src_label = label_map.get(src_id) or self._label_cache.get(src_id) or self._get_node_label(src_id, conn)
            dst_label = label_map.get(dst_id) or self._label_cache.get(dst_id) or self._get_node_label(dst_id, conn)

            if not src_label or not dst_label:
                continue

            key = (rel_type, src_label, dst_label)
            groups.setdefault(key, []).append((src_id, dst_id))

        csv_dir = self._get_csv_dir()

        for (rel_type, src_label, dst_label), pairs in groups.items():
            csv_path = os.path.join(csv_dir, f"_bulk_edges_{rel_type}_{src_label}_{dst_label}.csv")
            try:
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    for src_id, dst_id in pairs:
                        writer.writerow([
                            str(src_id).replace('\n', ' ').replace('\r', ''),
                            str(dst_id).replace('\n', ' ').replace('\r', '')
                        ])

                r = conn.execute(
                    f"COPY {rel_type} FROM '{csv_path}' (HEADER=false, PARALLEL=false, FROM='{src_label}', TO='{dst_label}', ESCAPE='\"', QUOTE='\"', DELIM=',', AUTO_DETECT=false)"
                )
                r.close()
            except Exception as e:
                print(
                    f"[ERROR] Failed to COPY edges {src_label}-[:{rel_type}]->{dst_label}: {e}",
                    file=sys.stderr,
                )
                sys.stderr.flush()
            finally:
                if os.path.exists(csv_path):
                    os.remove(csv_path)

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
            except Exception:
                pass

    def pagerank(self) -> list:
        try:
            results = []
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    self._ensure_schema(conn)
                    
                    # Load the algo extension dynamically
                    ext_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "modules", "gorgonzola", "extension", "algo", "build", "libalgo.gorgonzola_extension"
                    )
                    conn.execute(f"LOAD EXTENSION '{ext_path}';")
                    
                    # Project graph
                    try:
                        conn.execute("CALL DROP_PROJECTED_GRAPH('CodeGraph');")
                    except Exception:
                        pass
                        
                    conn.execute("""
                        CALL PROJECT_GRAPH('CodeGraph', 
                            ['File', 'Class', 'Method', 'Function', 'Interface', 'Symbol', 'Module'],
                            ['DEPENDS_ON', 'CONTAINS', 'EXTENDS', 'IMPLEMENTS', 'CALLS']
                        );
                    """)
                    
                    res = conn.execute("CALL page_rank('CodeGraph') RETURN node.id AS node_id, rank AS score;")
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
            sys.stderr.write(f"[WARNING] PageRank calculation failed: {e}\n")
            sys.stderr.flush()
            return []
