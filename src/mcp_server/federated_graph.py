import logging
import os
import shutil
import tempfile
import threading

from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
from src.mcp_server.graph_api import GraphAPI
from src.mcp_server.registry import registry

logger = logging.getLogger(__name__)

class FederatedGraphAPI(GraphAPI):
    """
    Dynamically builds an in-memory Kùzu instance by merging the graphs 
    of all registered repositories. This allows cross-boundary call graphs.
    """
    _federated_lock = threading.Lock()
    _cached_instance = None
    _cached_hashes = set()

    def __init__(self, target_repo_path: str = None):
        with self._federated_lock:
            repos = registry.get_all_repos()
            current_hashes = {r['hash'] for r in repos}

            # If we already built a federated graph for this exact set of repos, reuse it.
            if FederatedGraphAPI._cached_instance and current_hashes == FederatedGraphAPI._cached_hashes:
                # Use the cached in-memory graph
                super().__init__(repo_path=target_repo_path, graph=FederatedGraphAPI._cached_instance)
                return

            # Otherwise, build a new one
            logger.info("Building federated graph for %d repositories...", len(repos))
            from src.mcp_server.config import settings
            federated_path = os.path.join(settings.index_dir, "federated_kuzu")

            # Wipe old federated graph to rebuild cleanly
            if os.path.exists(federated_path):
                if os.path.isdir(federated_path):
                    shutil.rmtree(federated_path, ignore_errors=True)
                else:
                    try:
                        os.remove(federated_path)
                    except OSError:
                        pass

            in_memory_graph = GorgonzolaGraph(federated_path)
            # Ensure schema is created
            with in_memory_graph:
                pass

            temp_dir = tempfile.mkdtemp(prefix="pecorino_federated_")
            try:
                node_tables = ["File", "Class", "Method", "Function", "Interface", "Symbol", "Module", "ControlFlow", "Lambda", "Variable"]

                from src.mcp_server.gorgonzola_graph import _RELATIONSHIP_SCHEMA
                rel_pairs = {}
                rel_tables = []
                for schema in _RELATIONSHIP_SCHEMA:
                    table_name = schema.split("CREATE REL TABLE ")[1].split(" (")[0]
                    rel_tables.append(table_name)
                    pairs_str = schema.split("(")[1].split(")")[0].split(",")
                    parsed_pairs = []
                    for pair in pairs_str:
                        parts = pair.strip().split(" ")
                        if len(parts) >= 4 and parts[0] == "FROM" and parts[2] == "TO":
                            parsed_pairs.append((parts[1], parts[3]))
                    rel_pairs[table_name] = parsed_pairs

                # We will merge CSVs from all repos into single CSVs per actual table, then load them once.
                # To avoid ID collisions (if any, though IDs are usually hash-based or file-based),
                # we just append them.
                merged_node_csv = os.path.join(temp_dir, "CodeNode_merged.csv")
                merged_rel_csvs = {t: os.path.join(temp_dir, f"{t}_merged.csv") for t in rel_tables}
                
                import csv

                seen_node_ids = set()
                seen_edges = {}
                for table, pairs in rel_pairs.items():
                    for from_table, to_table in pairs:
                        seen_edges[f"{table}_{from_table}_{to_table}"] = set()

                for repo in repos:
                    logger.debug("Exporting graph for %s", repo['name'])
                    graph_path = repo['kuzu_path']
                    if not os.path.exists(graph_path):
                        continue

                    on_disk_graph = GorgonzolaGraph(graph_path)
                    try:
                        with on_disk_graph:
                            conn = on_disk_graph._conn
                            # Export all nodes in one go from CodeNode table
                            out_csv = os.path.join(temp_dir, f"CodeNode_{repo['hash']}.csv")
                            try:
                                cols = "a.id, a.name, a.node_type, a.filepath, a.start_line, a.end_line, a.complexity, a.extension, a.content_hash, a.mtime, a.lang, a.http_method, a.path, a.cf_type"
                                q = f"COPY (MATCH (a:CodeNode) RETURN {cols}) TO '{out_csv}'"
                                conn.execute(q)
                                if os.path.exists(out_csv):
                                    with open(out_csv, newline='') as infile:
                                        with open(merged_node_csv, 'a', newline='') as outfile:
                                            reader = csv.reader(infile)
                                            writer = csv.writer(outfile)
                                            for row in reader:
                                                if not row: continue
                                                node_id = row[0]
                                                if not node_id: continue
                                                if node_id not in seen_node_ids:
                                                    seen_node_ids.add(node_id)
                                                    writer.writerow(row)
                            except Exception as e:
                                logger.warning("Failed to export nodes: %s", e)

                            # Export rels per valid pair
                            for table, pairs in rel_pairs.items():
                                for from_table, to_table in pairs:
                                    out_csv = os.path.join(temp_dir, f"{table}_{from_table}_{to_table}_{repo['hash']}.csv")
                                    try:
                                        q = on_disk_graph._rewrite_cypher_query(f"COPY (MATCH (a:{from_table})-[e:{table}]->(b:{to_table}) RETURN a.id, b.id, e.*) TO '{out_csv}'")
                                        conn.execute(q)
                                        if os.path.exists(out_csv):
                                            key = f"{table}_{from_table}_{to_table}"
                                            with open(out_csv, newline='') as infile:
                                                with open(merged_rel_csvs[table], 'a', newline='') as outfile:
                                                    reader = csv.reader(infile)
                                                    writer = csv.writer(outfile)

                                                    for row in reader:
                                                        if not row: continue
                                                        if len(row) >= 2:
                                                            if not row[0] or not row[1]: continue
                                                            edge_id = (row[0], row[1])
                                                            if edge_id not in seen_edges[key]:
                                                                seen_edges[key].add(edge_id)
                                                                writer.writerow(row)
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.warning("Failed to export graph for %s: %s", repo['name'], e)

                # Import into in-memory DB
                with in_memory_graph:
                    conn = in_memory_graph._conn
                    
                    if os.path.exists(merged_node_csv) and os.path.getsize(merged_node_csv) > 0:
                        try:
                            conn.execute(f"COPY CodeNode FROM '{merged_node_csv}' (HEADER=false, ESCAPE='\"', QUOTE='\"', DELIM=',')")
                        except Exception as e:
                            logger.warning("Failed to import CodeNode: %s", e)

                    for table in rel_tables:
                        merged_csv = merged_rel_csvs[table]
                        if os.path.exists(merged_csv) and os.path.getsize(merged_csv) > 0:
                            try:
                                conn.execute(f"COPY {table} FROM '{merged_csv}' (FROM='CodeNode', TO='CodeNode', HEADER=false, ESCAPE='\"', QUOTE='\"', DELIM=',')")
                            except Exception as e:
                                logger.warning("Failed to import %s: %s", table, e)

                # Cache the federated graph
                if getattr(FederatedGraphAPI, '_cached_instance', None):
                    try:
                        FederatedGraphAPI._cached_instance.close()
                    except:
                        pass
                FederatedGraphAPI._cached_instance = in_memory_graph
                FederatedGraphAPI._cached_hashes = current_hashes

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            super().__init__(repo_path=target_repo_path, graph=FederatedGraphAPI._cached_instance)
