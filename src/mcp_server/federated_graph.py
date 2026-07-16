import logging
import os
import shutil
import tempfile
import threading
import json
import csv

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
            federated_path = os.path.join(settings.index_dir, "federated_gorgonzola")

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
                merged_identifier_csv = os.path.join(temp_dir, "Identifier_merged.csv")
                merged_rel_csvs = {t: os.path.join(temp_dir, f"{t}_merged.csv") for t in rel_tables}
                
                import csv

                seen_node_ids = set()
                seen_ident_ids = set()
                seen_edges = {}
                for table, pairs in rel_pairs.items():
                    for from_table, to_table in pairs:
                        seen_edges[f"{table}_{from_table}_{to_table}"] = set()

                for repo in repos:
                    logger.debug("Exporting graph for %s", repo['name'])
                    graph_path = repo['gorgonzola_path']
                    if not os.path.exists(graph_path):
                        continue

                    on_disk_graph = GorgonzolaGraph(graph_path)
                    try:
                        with on_disk_graph:
                            conn = on_disk_graph._conn
                            # Export all nodes in one go from CodeNode table
                            try:
                                cols = "a.id, a.kind, a.name, a.qualified_name, a.file, a.line, a.end_line, a.mtime, a.complexity, a.docstring, a.embedding"
                                q = f"MATCH (a:CodeNode) RETURN {cols}"
                                res = conn.execute(q)
                                with open(merged_node_csv, 'a', newline='', encoding='utf-8') as outfile:
                                    writer = csv.writer(outfile, quoting=csv.QUOTE_MINIMAL, quotechar='"', escapechar='"')
                                    while res.has_next():
                                        row = res.get_next()
                                        if not row: continue
                                        node_id = row[0]
                                        if not node_id: continue
                                        if node_id not in seen_node_ids:
                                            seen_node_ids.add(node_id)
                                            # Format lists as JSON string for CSV
                                            formatted_row = [json.dumps(x) if isinstance(x, list) else x for x in row]
                                            writer.writerow(formatted_row)
                                res.close()
                            except Exception as e:
                                logger.warning("Failed to export nodes: %s", e)

                            # Export Identifiers
                            try:
                                cols_ident = "a.id, a.raw, a.tokens, a.case_style, a.prefix, a.suffix, a.verb, a.entity, a.qualifier, a.is_magic, a.canonical_verb, a.canonical_entity, a.domain, a.intent, a.embedding"
                                q_ident = f"MATCH (a:Identifier) RETURN {cols_ident}"
                                res = conn.execute(q_ident)
                                with open(merged_identifier_csv, 'a', newline='', encoding='utf-8') as outfile:
                                    writer = csv.writer(outfile, quoting=csv.QUOTE_MINIMAL, quotechar='"', escapechar='"')
                                    while res.has_next():
                                        row = res.get_next()
                                        if not row: continue
                                        node_id = row[0]
                                        if not node_id: continue
                                        if node_id not in seen_ident_ids:
                                            seen_ident_ids.add(node_id)
                                            formatted_row = [json.dumps(x) if isinstance(x, list) else x for x in row]
                                            writer.writerow(formatted_row)
                                res.close()
                            except Exception as e:
                                logger.warning("Failed to export Identifiers: %s", e)

                            # Export rels per valid pair
                            for table, pairs in rel_pairs.items():
                                for from_table, to_table in pairs:
                                    try:
                                        q = on_disk_graph._rewrite_cypher_query(f"MATCH (a:{from_table})-[e:{table}]->(b:{to_table}) RETURN a.id, b.id, e.*")
                                        res = conn.execute(q)
                                        key = f"{table}_{from_table}_{to_table}"
                                        with open(merged_rel_csvs[table], 'a', newline='', encoding='utf-8') as outfile:
                                            writer = csv.writer(outfile, quoting=csv.QUOTE_MINIMAL, quotechar='"', escapechar='"')
                                            while res.has_next():
                                                row = res.get_next()
                                                if not row: continue
                                                if len(row) >= 2:
                                                    if not row[0] or not row[1]: continue
                                                    edge_id = (row[0], row[1])
                                                    if edge_id not in seen_edges[key]:
                                                        seen_edges[key].add(edge_id)
                                                        writer.writerow(row)
                                        res.close()
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

                    if os.path.exists(merged_identifier_csv) and os.path.getsize(merged_identifier_csv) > 0:
                        try:
                            conn.execute(f"COPY Identifier FROM '{merged_identifier_csv}' (HEADER=false, ESCAPE='\"', QUOTE='\"', DELIM=',')")
                        except Exception as e:
                            logger.warning("Failed to import Identifier: %s", e)

                    for table in rel_tables:
                        merged_csv = merged_rel_csvs[table]
                        if os.path.exists(merged_csv) and os.path.getsize(merged_csv) > 0:
                            try:
                                pairs = rel_pairs.get(table, [("CodeNode", "CodeNode")])
                                from_table, to_table = pairs[0]
                                conn.execute(f"COPY {table} FROM '{merged_csv}' (FROM='{from_table}', TO='{to_table}', HEADER=false, ESCAPE='\"', QUOTE='\"', DELIM=',')")
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
