import os
import threading
from typing import Any, Dict, List

from src.core.errors import AnalysisError
from src.mcp_server.gorgonzola_graph import GorgonzolaGraph
from src.mcp_server.index_db import find_repo_root, get_db_path_for_repo, get_graph_path_for_repo


class GraphAPI:
    def __init__(self, repo_path: str = None, graph: GorgonzolaGraph = None):
        self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
        self.db_path = get_db_path_for_repo(self.repo_path)
        if graph is not None:
            self.graph = graph
            self._owns_graph = False
        else:
            self.graph = GorgonzolaGraph(db_path=get_graph_path_for_repo(self.db_path))
            self._owns_graph = True
        self._pagerank_cache = None
        self._pagerank_lock = threading.Lock()

    def close(self):
        """Close the graph connection if this instance owns it."""
        if self._owns_graph and self.graph is not None:
            try:
                self.graph.close()
            except Exception:
                pass
            self.graph = None

    def invalidate_pagerank_cache(self):
        with self._pagerank_lock:
            self._pagerank_cache = None

    def _safe_query(self, fn):
        """Run fn() and return (result, None) on success, or (None, error_str) on failure."""
        try:
            return fn(), None
        except Exception as e:
            return None, str(e)

    def get_file_dependencies(self, filepath: str) -> Dict[str, Any]:
        """Query incoming and outgoing file dependencies and PageRank score."""
        rel_depends = "DEPENDS_ON"
        error_msg = None

        incoming_deps, err = self._safe_query(
            lambda: [r["id"] for r in self.graph.query(
                f"MATCH (other:CodeNode {{node_type: 'File'}})-[:{rel_depends}]->(f:CodeNode {{node_type: 'File', id: $id}}) RETURN other.id AS id",
                {"id": filepath}
            )]
        )
        if err:
            incoming_deps = []
            error_msg = err

        outgoing_deps = []
        if not error_msg:
            outgoing_deps, err = self._safe_query(
                lambda: [{"id": r["id"], "type": r["label"] if r.get("label") else "Unknown"} for r in self.graph.query(
                    f"MATCH (f:CodeNode {{node_type: 'File', id: $id}})-[:{rel_depends}]->(other) RETURN other.id AS id, other.node_type AS label",
                    {"id": filepath}
                )]
            )
            if err:
                outgoing_deps = []
                error_msg = err

        pagerank_score = 0.0
        if not error_msg:
            def _get_pagerank():
                with self._pagerank_lock:
                    if self._pagerank_cache is None:
                        pr_scores = self.graph.pagerank()
                        self._pagerank_cache = {pr.get("node_id"): pr.get("score", 0.0) for pr in pr_scores}
                    return self._pagerank_cache.get(filepath, 0.0)
            pagerank_score, err = self._safe_query(_get_pagerank)
            if err:
                pagerank_score = 0.0
                error_msg = err

        res = {
            "graph_status": "ok" if not error_msg else "unavailable",
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
                raw = row.get(return_key, row)
                if isinstance(raw, dict) and 'properties' in raw:
                    props = raw['properties']
                else:
                    props = raw
                node_id = props.get('id', str(props))
                if node_id not in seen:
                    seen.add(node_id)
                    unique_nodes.append(props)
            return unique_nodes
        except Exception as e:
            raise AnalysisError(f"Graph query failed for '{target_name}': {e}") from e

    def _name_match_clause(self, var: str, param: str = "$target") -> str:
        """Build a Cypher WHERE clause that fuzzy-matches both simple and class-qualified names.

        Handles queries like 'index_directory' matching nodes named
        'CodebaseIndexer.index_directory' or 'index_directory'.
        """
        return (
            f"({var}.name = {param} "
            f"OR ends_with({var}.name, '.' + {param}) "
            f"OR {var}.name CONTAINS {param})"
        )

    def find_callers(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all methods/functions that call the target function/method.

        Searches both resolved CALLS edges and unresolved Symbol nodes.
        """
        where = self._name_match_clause("callee")
        # Direct CALLS to Method/Function nodes
        q1 = f"MATCH (caller)-[:CALLS]->(callee) WHERE {where} RETURN caller"
        results = self._find_calls(q1, target_name, 'caller')

        # Also search via Symbol nodes (most CALLS edges are still unresolved)
        where_sym = self._name_match_clause("s")
        q2 = f"MATCH (caller)-[:CALLS]->(s:CodeNode {{node_type: 'Symbol'}}) WHERE {where_sym} RETURN caller"
        results.extend(self._find_calls(q2, target_name, 'caller'))

        # Deduplicate
        seen = set()
        unique = []
        for r in results:
            rid = r.get('id', str(r))
            if rid not in seen:
                seen.add(rid)
                unique.append(r)
        return unique

    def find_callees(self, target_name: str) -> List[Dict[str, Any]]:
        """Find all functions/methods called by the given target function/method.

        Returns both resolved targets and unresolved Symbol names.
        """
        where = self._name_match_clause("caller")
        q1 = f"MATCH (caller)-[:CALLS]->(callee) WHERE {where} RETURN callee"
        results = self._find_calls(q1, target_name, 'callee')

        seen = set()
        unique = []
        for r in results:
            rid = r.get('id', str(r))
            if rid not in seen:
                seen.add(rid)
                unique.append(r)
        return unique

    def trace_calls(self, symbol: str, direction: str = "both", max_depth: int = 3) -> Dict[str, Any]:
        """Multi-hop call graph traversal, returning a tree with hop distances.

        Similar to CBM's trace_path. Returns callers and/or callees
        at each hop level up to max_depth.
        """
        result = {"function": symbol, "direction": direction}
        where = self._name_match_clause("start")
        max_depth = min(max_depth, 10)

        if direction in ("outbound", "both"):
            callees = []
            for depth in range(1, max_depth + 1):
                try:
                    if depth == 1:
                        q = (
                            f"MATCH (start)-[e:CALLS|LIKELY_CALLS|DATA_FLOWS_TO]->(target) "
                            f"WHERE {where} "
                            f"RETURN target.name AS name, target.id AS id, "
                            f"labels(target) AS label, e"
                        )
                    else:
                        q = (
                            f"MATCH (start)-[:CALLS|LIKELY_CALLS|DATA_FLOWS_TO*{depth - 1}..{depth - 1}]->(mid)-[e:CALLS|LIKELY_CALLS|DATA_FLOWS_TO]->(target) "
                            f"WHERE {where} "
                            f"RETURN target.name AS name, target.id AS id, "
                            f"labels(target) AS label, e"
                        )
                    rows = self.graph.query(q, {"target": symbol})
                    for row in rows:
                        edge_info = row.get("e", {})
                        if isinstance(edge_info, dict):
                            edge_type = edge_info.get("_label", "CALLS")
                            confidence = edge_info.get("confidence")
                        else:
                            edge_type = "CALLS"
                            confidence = None
                            
                        callees.append({
                            "name": row.get("name", ""),
                            "id": row.get("id", ""),
                            "label": row.get("label", ""),
                            "hop": depth,
                            "edge_type": edge_type,
                            "confidence": confidence
                        })
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).debug("trace_calls outbound depth=%d failed: %s", depth, e)
                    break
            result["callees"] = callees

        if direction in ("inbound", "both"):
            callers = []
            for depth in range(1, max_depth + 1):
                try:
                    if depth == 1:
                        q = (
                            f"MATCH (source)-[e:CALLS|LIKELY_CALLS|DATA_FLOWS_TO]->(target) "
                            f"WHERE {self._name_match_clause('target')} "
                            f"RETURN source.name AS name, source.id AS id, "
                            f"labels(source) AS label, e"
                        )
                    else:
                        q = (
                            f"MATCH (source)-[e:CALLS|LIKELY_CALLS|DATA_FLOWS_TO]->(mid)-[:CALLS|LIKELY_CALLS|DATA_FLOWS_TO*{depth - 1}..{depth - 1}]->(target) "
                            f"WHERE {self._name_match_clause('target')} "
                            f"RETURN source.name AS name, source.id AS id, "
                            f"labels(source) AS label, e"
                        )
                    rows = self.graph.query(q, {"target": symbol})
                    for row in rows:
                        edge_info = row.get("e", {})
                        if isinstance(edge_info, dict):
                            edge_type = edge_info.get("_label", "CALLS")
                            confidence = edge_info.get("confidence")
                        else:
                            edge_type = "CALLS"
                            confidence = None
                            
                        callers.append({
                            "name": row.get("name", ""),
                            "id": row.get("id", ""),
                            "label": row.get("label", ""),
                            "hop": depth,
                            "edge_type": edge_type,
                            "confidence": confidence
                        })
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).debug("trace_calls inbound depth=%d failed: %s", depth, e)
                    break
            result["callers"] = callers

        return result

    def impact_analysis(self, filepath: str, max_depth: int = 3) -> List[Dict[str, Any]]:
        """Find all files/modules that transitively depend on the given filepath."""
        query = f'''
            MATCH (dependent)-[:DEPENDS_ON*1..{max_depth}]->(f:CodeNode {{node_type: 'File', id: $path}})
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
            raise AnalysisError(f"Impact analysis failed for '{filepath}': {e}") from e

    def resolve_symbols(self):
        """
        Post-indexing step to resolve dangling 'Symbol' nodes into actual 'Method' or 'Function' nodes.
        In a full implementation, this would handle import-qualified resolution.
        For now, it does direct name matching.
        """
        queries = [
            '''
            MATCH (caller:CodeNode {node_type: 'Method'})-[r:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Method', name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            DELETE r
            ''',
            '''
            MATCH (caller:CodeNode {node_type: 'Method'})-[r:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Function', name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            DELETE r
            ''',
            '''
            MATCH (caller:CodeNode {node_type: 'Function'})-[r:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Method', name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            DELETE r
            ''',
            '''
            MATCH (caller:CodeNode {node_type: 'Function'})-[r:CALLS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Function', name: s.name})
            MERGE (caller)-[:CALLS]->(target)
            DELETE r
            ''',
            '''
            MATCH (c:CodeNode {node_type: 'Class'})-[r:EXTENDS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Class', name: s.name})
            MERGE (c)-[:EXTENDS]->(target)
            DELETE r
            ''',
            '''
            MATCH (c:CodeNode {node_type: 'Class'})-[r:IMPLEMENTS]->(s:CodeNode {node_type: 'Symbol'}), (target:CodeNode {node_type: 'Interface', name: s.name})
            MERGE (c)-[:IMPLEMENTS]->(target)
            DELETE r
            '''
        ]
        self.graph.query_batch(queries)

        # 2. Semantic Fallback for unresolved symbols
        unresolved_q = '''
        MATCH (caller)-[r:CALLS]->(s:CodeNode {node_type: 'Symbol'})
        RETURN caller.id AS caller_id, s.name AS symbol_name, s.id AS symbol_id
        LIMIT 100
        '''
        unresolved = self.graph.query(unresolved_q)
        if unresolved:
            from src.mcp_server.index_db import CodeSearchIndex
            # Need to avoid blocking the graph during FTS query, so we do it carefully
            index = CodeSearchIndex(db_path=self.db_path, read_only=True)
            try:
                for row in unresolved:
                    symbol_name = row.get("symbol_name")
                    caller_id = row.get("caller_id")
                    if not symbol_name or not caller_id:
                        continue
                    
                    # Find semantically closest node
                    res = index.search(symbol_name, limit=1, mode="hybrid")
                    if res:
                        best_match = res[0]
                        target_id = best_match.get("id")
                        score = best_match.get("score", 0.0)
                        
                        # Only link if the match is decent and not self
                        if target_id and target_id != caller_id and score > 0.3:
                            q = '''
                            MATCH (caller {id: $caller_id}), (target {id: $target_id})
                            MERGE (caller)-[:LIKELY_CALLS {confidence: $score}]->(target)
                            '''
                            self.graph.query(q, {"caller_id": caller_id, "target_id": target_id, "score": score})
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Semantic fallback resolution failed: {e}")
            finally:
                index.close()

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
            MATCH (f:CodeNode {node_type: 'File'})-[:CONTAINS*1..3]->(m)-[:CONTAINS_LAMBDA]->(l:CodeNode {node_type: 'Lambda'})
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
            raise AnalysisError(f"Functional purity analysis failed: {e}") from e

        return result
