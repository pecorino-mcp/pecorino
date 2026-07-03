import os
import threading
from typing import List, Dict, Any
from src.mcp_server.index_db import get_db_path_for_repo, find_repo_root
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph

class GraphAPI:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        self.db_path = get_db_path_for_repo(self.repo_path)
        self.graph = GorgonzolaGraph(db_path=self.db_path)
        self._pagerank_cache = None
        self._pagerank_lock = threading.Lock()

    def invalidate_pagerank_cache(self):
        with self._pagerank_lock:
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
                with self._pagerank_lock:
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

    def _find_calls(self, query: str, target_name: str, return_key: str) -> List[Dict[str, Any]]:
        try:
            results = self.graph.query(query, {"target": target_name})
            seen = set()
            unique_nodes = []
            for row in results:
                props = row[return_key]['properties']
                if props.get('id') not in seen:
                    seen.add(props.get('id'))
                    unique_nodes.append(props)
            return unique_nodes
        except Exception as e:
            return [{"error": str(e), "type": "graph_query_failed"}]

    def find_callers(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all methods/functions that call the target function/method."""
        query = '''
            MATCH (caller)-[:CALLS]->(callee)
            WHERE callee.name = $target
            RETURN caller
        '''
        return self._find_calls(query, target_name, 'caller')

    def find_callees(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all functions/methods called by the given target function/method."""
        query = '''
            MATCH (caller)-[:CALLS]->(callee)
            WHERE caller.name = $target
            RETURN callee
        '''
        return self._find_calls(query, target_name, 'callee')

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

    def analyze_functional_purity(self) -> Dict[str, Any]:
        """Aggregate functional purity metrics: Lambda density, State mutations, Recursive loops."""
        result = {
            "lambda_density": [],
            "state_mutation_hotspots": [],
            "recursive_loops": []
        }
        try:
            # 1. Lambda Density
            q_lambdas = '''
            MATCH (f:File)-[:CONTAINS*1..3]->(m)-[:CONTAINS_LAMBDA]->(l:Lambda)
            RETURN f.name AS file, COUNT(l) AS lambda_count
            ORDER BY lambda_count DESC LIMIT 20
            '''
            for row in self.graph.query(q_lambdas):
                result["lambda_density"].append({
                    "file": row.get("file"),
                    "count": row.get("lambda_count")
                })
            
            # 2. State Mutation Hotspots
            q_mutations = '''
            MATCH (m)-[a:ACCESSES_STATE]->(v:Variable)
            WHERE a.is_mutation = true OR a.is_taint = true
            RETURN m.name AS function_name, COUNT(v) AS mutation_count
            ORDER BY mutation_count DESC LIMIT 20
            '''
            for row in self.graph.query(q_mutations):
                result["state_mutation_hotspots"].append({
                    "function": row.get("function_name"),
                    "mutation_count": row.get("mutation_count")
                })
                
            # 3. Recursive Loops
            q_recursion = '''
            MATCH (m)-[:RECURSES_TO]->(m)
            RETURN m.name AS recursive_function, m.filepath AS filepath
            LIMIT 50
            '''
            for row in self.graph.query(q_recursion):
                result["recursive_loops"].append({
                    "function": row.get("recursive_function"),
                    "filepath": row.get("filepath")
                })
                
        except Exception as e:
            result["error"] = str(e)
            
        return result
