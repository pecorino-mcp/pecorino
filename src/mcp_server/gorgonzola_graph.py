import os
import sys

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
            "CREATE NODE TABLE Class (id STRING, name STRING, filepath STRING, body_text STRING, start_line INT64, end_line INT64, metrics_json STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Method (id STRING, name STRING, complexity INT64, filepath STRING, body_text STRING, start_line INT64, end_line INT64, metrics_json STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Function (id STRING, name STRING, complexity INT64, filepath STRING, body_text STRING, start_line INT64, end_line INT64, metrics_json STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Interface (id STRING, name STRING, filepath STRING, body_text STRING, start_line INT64, end_line INT64, metrics_json STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Symbol (id STRING, name STRING, PRIMARY KEY (id))",
            "CREATE NODE TABLE Module (id STRING, name STRING, PRIMARY KEY (id))",

            # Create relationship tables
            "CREATE REL TABLE CONTAINS (FROM File TO Class, FROM Class TO Method, FROM File TO Interface, FROM Interface TO Method, FROM File TO Function)",
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
        
        with gorgonzola.Database(self.gorgonzola_db_path) as db:
            with gorgonzola.Connection(db) as conn:
                init_gorgonzola_schema(conn)

    def _get_node_label(self, node_id: str, conn) -> str:
        tables = ["File", "Class", "Method", "Function", "Interface", "Symbol", "Module"]
        for t in tables:
            res = conn.execute(f"MATCH (n:{t} {{id: $id}}) RETURN label(n) AS lbl", {"id": node_id})
            lbl = None
            if res.has_next():
                lbl = res.get_next()[0]
            res.close()
            if lbl:
                return lbl
        return None

    def query(self, query: str, parameters: dict = None) -> list:
        if parameters is None:
            parameters = {}
        
        with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
            with self.gorgonzola.Connection(db) as conn:
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
                properties = {key: val for key, val in v.items() if not key.startswith("_")}
                return {
                    "properties": properties,
                    "labels": [v[label_key]],
                    "label": v[label_key]
                }
            elif ("_src" in v and "_dst" in v) or ("_SRC" in v and "_DST" in v):
                label_key = "_label" if "_label" in v else "_LABEL"
                properties = {key: val for key, val in v.items() if not key.startswith("_")}
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
        # nodes is a list of (node_id, properties, label)
        with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
            with self.gorgonzola.Connection(db) as conn:
                for node_id, properties, label in nodes:
                    params = {"id": node_id}
                    prop_assignments = []
                    for k, v in properties.items():
                        if v is not None:
                            if k in ("complexity", "start_line", "end_line"):
                                params[k] = int(v)
                            elif k in ("mtime",):
                                params[k] = float(v)
                            else:
                                params[k] = str(v)
                            prop_assignments.append(f"n.{k} = ${k}")
                    
                    prop_str = ", ".join(prop_assignments)
                    if prop_str:
                        query = f"MERGE (n:{label} {{id: $id}}) ON CREATE SET {prop_str} ON MATCH SET {prop_str}"
                    else:
                        query = f"MERGE (n:{label} {{id: $id}})"
                    
                    r = conn.execute(query, params)
                    r.close()
            
        return {node_id: node_id for node_id, _, _ in nodes}

    def insert_edges_bulk(self, edges, id_map=None):
        # edges is a list of (src_id, dst_id, properties, rel_type)
        with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
            with self.gorgonzola.Connection(db) as conn:
                for src_id, dst_id, properties, rel_type in edges:
                    src_label = self._get_node_label(src_id, conn)
                    dst_label = self._get_node_label(dst_id, conn)
                    
                    if not src_label or not dst_label:
                        continue
                        
                    query = f"MATCH (src:{src_label} {{id: $src}}), (dst:{dst_label} {{id: $dst}}) MERGE (src)-[:{rel_type}]->(dst)"
                    r = conn.execute(query, {"src": src_id, "dst": dst_id})
                    r.close()

    def pagerank(self) -> list:
        try:
            import networkx as nx
            nx_graph = nx.DiGraph()
            
            with self.gorgonzola.Database(self.gorgonzola_db_path) as db:
                with self.gorgonzola.Connection(db) as conn:
                    # Get all Files
                    files_res = conn.execute("MATCH (f:File) RETURN f.id AS id")
                    while files_res.has_next():
                        nx_graph.add_node(files_res.get_next()[0])
                    files_res.close()
                        
                    # Get all DEPENDS_ON relations
                    res = conn.execute("MATCH (a:File)-[:DEPENDS_ON]->(b:File) RETURN a.id AS src, b.id AS dst")
                    while res.has_next():
                        row = res.get_next()
                        nx_graph.add_edge(row[0], row[1])
                    res.close()
                        
            if len(nx_graph) == 0:
                return []
                
            pr = nx.pagerank(nx_graph)
            return [{"node_id": node, "score": score} for node, score in pr.items()]
        except Exception as e:
            sys.stderr.write(f"[WARNING] PageRank calculation failed: {e}\n")
            sys.stderr.flush()
            return []
