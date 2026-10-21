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
    Dynamically builds a federated SQLite graph instance by merging the graphs
    of all registered repositories. This allows cross-boundary call graphs.
    """

    _federated_lock = threading.Lock()
    _cached_instance = None
    _cached_hashes = set()

    def __init__(self, target_repo_path: str = None):
        with self._federated_lock:
            repos = registry.get_all_repos()
            current_hashes = {r["hash"] for r in repos}

            if (
                FederatedGraphAPI._cached_instance
                and current_hashes == FederatedGraphAPI._cached_hashes
            ):
                super().__init__(
                    repo_path=target_repo_path, graph=FederatedGraphAPI._cached_instance
                )
                return

            logger.info("Building federated graph for %d repositories...", len(repos))
            federated_path = os.path.join(
                tempfile.gettempdir(), "pecorino_federated_gorgonzola"
            )

            if os.path.exists(federated_path):
                if os.path.isdir(federated_path):
                    shutil.rmtree(federated_path, ignore_errors=True)
                else:
                    try:
                        os.remove(federated_path)
                    except OSError:
                        pass

            in_memory_graph = GorgonzolaGraph(federated_path)
            with in_memory_graph:
                dst_conn = in_memory_graph._conn.raw_conn
                for repo in repos:
                    graph_path = repo.get("gorgonzola_path")
                    if not graph_path or not os.path.exists(graph_path):
                        continue
                    try:
                        on_disk_graph = GorgonzolaGraph(graph_path)
                        with on_disk_graph:
                            src_conn = on_disk_graph._conn.raw_conn
                            nodes = src_conn.execute("SELECT * FROM graph_nodes").fetchall()
                            if nodes:
                                placeholders = ",".join("?" for _ in nodes[0])
                                dst_conn.executemany(
                                    f"INSERT OR IGNORE INTO graph_nodes VALUES ({placeholders})",
                                    nodes,
                                )
                            idents = src_conn.execute("SELECT * FROM graph_identifiers").fetchall()
                            if idents:
                                placeholders = ",".join("?" for _ in idents[0])
                                dst_conn.executemany(
                                    f"INSERT OR IGNORE INTO graph_identifiers VALUES ({placeholders})",
                                    idents,
                                )
                            edges = src_conn.execute("SELECT * FROM graph_edges").fetchall()
                            if edges:
                                placeholders = ",".join("?" for _ in edges[0])
                                dst_conn.executemany(
                                    f"INSERT OR IGNORE INTO graph_edges VALUES ({placeholders})",
                                    edges,
                                )
                    except Exception as e:
                        logger.warning(
                            "Failed to merge federated graph for %s: %s",
                            repo.get("name"),
                            e,
                        )
                dst_conn.commit()

            if getattr(FederatedGraphAPI, "_cached_instance", None):
                try:
                    FederatedGraphAPI._cached_instance.close()
                except Exception:
                    pass
            FederatedGraphAPI._cached_instance = in_memory_graph
            FederatedGraphAPI._cached_hashes = current_hashes

            super().__init__(
                repo_path=target_repo_path, graph=FederatedGraphAPI._cached_instance
            )
