import os
from typing import List, Dict, Any
from src.mcp_server.index_db import get_db_path_for_repo, find_repo_root
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

class GraphAPI:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        self.db_path = get_db_path_for_repo(self.repo_path)
        self.graph = GorgonzolaGraph(db_path=self.db_path)
        self._pagerank_cache = None

    def invalidate_pagerank_cache(self):
        self._pagerank_cache = None

    def get_file_dependencies(self, filepath: str) -> Dict[str, Any]:
        """Query incoming and outgoing file dependencies and PageRank score."""
        rel_depends = "DEPENDS_ON"
        graph_status = "ok"
        error_msg = None

        incoming_deps = []
        outgoing_deps = []
        pagerank_score = 0.0

        try:
            inc_res = self.graph.query(f"MATCH (other:File)-[:{rel_depends}]->(f:File {{id: $id}}) RETURN other.id AS id", {"id": filepath})
            incoming_deps = [r["id"] for r in inc_res]
        except Exception as e:
            graph_status = "unavailable"
            error_msg = str(e)

        if graph_status == "ok":
            try:
                out_res = self.graph.query(f"MATCH (f:File {{id: $id}})-[:{rel_depends}]->(other) RETURN other.id AS id, label(other) AS label", {"id": filepath})
                outgoing_deps = [{"id": r["id"], "type": r["label"] if r.get("label") else "Unknown"} for r in out_res]
            except Exception as e:
                graph_status = "unavailable"
                error_msg = str(e)

        if graph_status == "ok":
            try:
                if self._pagerank_cache is None:
                    pr_scores = self.graph.pagerank()
                    self._pagerank_cache = {pr.get("node_id"): pr.get("score", 0.0) for pr in pr_scores}
                pagerank_score = self._pagerank_cache.get(filepath, 0.0)
            except Exception as e:
                graph_status = "unavailable"
                error_msg = str(e)

        res = {
            "graph_status": graph_status,
            "incoming_dependencies": incoming_deps,
            "outgoing_dependencies": outgoing_deps,
            "pagerank_score": pagerank_score
        }
        if error_msg:
            res["error"] = error_msg
        return res

    def find_callers(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all methods/functions that call the target function/method."""
        query = '''
            MATCH (caller)-[:CALLS]->(callee)
            WHERE callee.name = $target
            RETURN caller
        '''
        try:
            results = self.graph.query(query, {"target": target_name})
            seen = set()
            unique_callers = []
            for row in results:
                props = row['caller']['properties']
                if props.get('id') not in seen:
                    seen.add(props.get('id'))
                    unique_callers.append(props)
            return unique_callers
        except Exception as e:
            return [{"error": str(e), "type": "graph_query_failed"}]

    def find_callees(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all functions/methods called by the given target function/method."""
        query = '''
            MATCH (caller)-[:CALLS]->(callee)
            WHERE caller.name = $target
            RETURN callee
        '''
        try:
            results = self.graph.query(query, {"target": target_name})
            seen = set()
            unique_callees = []
            for row in results:
                props = row['callee']['properties']
                if props.get('id') not in seen:
                    seen.add(props.get('id'))
                    unique_callees.append(props)
            return unique_callees
        except Exception as e:
            return [{"error": str(e), "type": "graph_query_failed"}]

    def impact_analysis(self, filepath: str, max_depth: int = 3) -> List[Dict[str, Any]]:
        """Find all files/modules that transitively depend on the given filepath."""
        query = f'''
            MATCH (dependent)-[:DEPENDS_ON*1..{max_depth}]->(f:File {{id: $path}})
            RETURN dependent
        '''
        try:
            results = self.graph.query(query, {"path": filepath})
            seen = set()
            unique_deps = []
            for row in results:
                dep_props = row['dependent']['properties']
                dep_name = dep_props.get('name', '')
                if dep_name not in seen:
                    seen.add(dep_name)
                    unique_deps.append(dep_props)
            return unique_deps
        except Exception as e:
            return [{"error": str(e), "type": "graph_query_failed"}]

    def resolve_symbols(self):
        """
        Post-indexing step to resolve dangling 'Symbol' nodes into actual 'Method' or 'Function' nodes.
        In a full implementation, this would handle import-qualified resolution.
        For now, it does direct name matching.
        """
        queries = [
            '''
            MATCH (caller:Method)-[r:CALLS]->(s:Symbol), (target:Method {name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            ''',
            '''
            MATCH (caller:Method)-[r:CALLS]->(s:Symbol), (target:Function {name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            ''',
            '''
            MATCH (caller:Function)-[r:CALLS]->(s:Symbol), (target:Method {name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            ''',
            '''
            MATCH (caller:Function)-[r:CALLS]->(s:Symbol), (target:Function {name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            ''',
            '''
            MATCH (c:Class)-[r:EXTENDS]->(s:Symbol), (target:Class {name: s.name})
            MERGE (c)-[:EXTENDS]->(target)
            ''',
            '''
            MATCH (c:Class)-[r:IMPLEMENTS]->(s:Symbol), (target:Interface {name: s.name})
            MERGE (c)-[:IMPLEMENTS]->(target)
            ''',
            '''
            MATCH (s:Symbol)
            DETACH DELETE s
            '''
        ]
        
        self.graph.query_batch(queries)
