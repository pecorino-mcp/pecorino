import os
import shutil
import tempfile
import threading
from typing import List, Dict

from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
from src.mcp_server.registry import registry
from src.mcp_server.graph_api import GraphAPI
import logging

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
            logger.info("Building federated in-memory graph for %d repositories...", len(repos))
            in_memory_graph = GorgonzolaGraph(":memory:")
            # Ensure schema is created
            with in_memory_graph:
                pass

            temp_dir = tempfile.mkdtemp(prefix="pecorino_federated_")
            try:
                node_tables = ["File", "Class", "Method", "Function", "Interface", "Symbol", "Module", "ControlFlow", "Lambda", "Variable"]
                rel_tables = ["CONTAINS", "CONTAINS_LAMBDA", "EXTENDS", "IMPLEMENTS", "CALLS", "RECURSES_TO", "DEPENDS_ON", "ACCESSES_STATE"]
                
                # We will merge CSVs from all repos into single CSVs per table, then load them once.
                # To avoid ID collisions (if any, though IDs are usually hash-based or file-based), 
                # we just append them.
                merged_csvs = {t: os.path.join(temp_dir, f"{t}_merged.csv") for t in node_tables + rel_tables}
                
                for repo in repos:
                    logger.debug("Exporting graph for %s", repo['name'])
                    graph_path = repo['kuzu_path']
                    if not os.path.exists(graph_path):
                        continue
                        
                    on_disk_graph = GorgonzolaGraph(graph_path)
                    try:
                        with on_disk_graph:
                            conn = on_disk_graph._conn
                            # Export nodes
                            for table in node_tables:
                                out_csv = os.path.join(temp_dir, f"{table}_{repo['hash']}.csv")
                                try:
                                    conn.execute(f"COPY (MATCH (a:{table}) RETURN a.*) TO '{out_csv}'")
                                    if os.path.exists(out_csv):
                                        with open(merged_csvs[table], 'a') as outfile:
                                            with open(out_csv, 'r') as infile:
                                                # Skip header if outfile already has content
                                                if os.path.getsize(merged_csvs[table]) > 0:
                                                    next(infile, None) 
                                                shutil.copyfileobj(infile, outfile)
                                except Exception as e:
                                    # Ignore if table is empty or doesn't exist
                                    pass
                                    
                            # Export rels
                            for table in rel_tables:
                                out_csv = os.path.join(temp_dir, f"{table}_{repo['hash']}.csv")
                                try:
                                    # We must return FROM and TO nodes by default Kuzu COPY format
                                    conn.execute(f"COPY (MATCH (a)-[e:{table}]->(b) RETURN id(a), id(b), e.*) TO '{out_csv}'")
                                    if os.path.exists(out_csv):
                                        with open(merged_csvs[table], 'a') as outfile:
                                            with open(out_csv, 'r') as infile:
                                                if os.path.getsize(merged_csvs[table]) > 0:
                                                    next(infile, None)
                                                shutil.copyfileobj(infile, outfile)
                                except Exception as e:
                                    pass
                    except Exception as e:
                        logger.warning("Failed to export graph for %s: %s", repo['name'], e)

                # Import into in-memory DB
                with in_memory_graph:
                    conn = in_memory_graph._conn
                    for table in node_tables:
                        if os.path.exists(merged_csvs[table]) and os.path.getsize(merged_csvs[table]) > 0:
                            try:
                                conn.execute(f"COPY {table} FROM '{merged_csvs[table]}' (HEADER=true)")
                            except Exception as e:
                                logger.warning("Failed to import %s: %s", table, e)
                                
                    for table in rel_tables:
                        if os.path.exists(merged_csvs[table]) and os.path.getsize(merged_csvs[table]) > 0:
                            try:
                                conn.execute(f"COPY {table} FROM '{merged_csvs[table]}' (HEADER=true)")
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
