import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class DSLCompiler:
    """
    Compiles JSON DSL queries into DuckDB SQL and Kùzu Cypher queries.
    """

    @staticmethod
    def compile(query_json: Dict[str, Any], db_path: str = "main") -> Tuple[Optional[str], Optional[str], Optional[list]]:
        """
        Compiles the query into (sql_query, cypher_query, sql_params).
        """
        select = query_json.get("select", "nodes")
        where = query_json.get("where", {})
        join_graph = query_json.get("join_graph", None)
        limit = min(query_json.get("limit", 20), 100)
        offset = max(query_json.get("offset", 0), 0)

        if select not in ("nodes", "files"):
            raise ValueError(f"Invalid select target: {select}")

        sql_parts = []
        sql_params = []

        if select == "nodes":
            base_query = f"SELECT id, name, node_type, filepath, start_line, end_line FROM {db_path}.code_nodes WHERE 1=1"
        else:
            base_query = f"SELECT filepath, lang, mtime FROM {db_path}.files WHERE 1=1"

        sql_parts.append(base_query)

        # Parse WHERE clauses
        for key, condition in where.items():
            if select == "nodes" and key not in ("node_type", "filepath", "name", "relationships"):
                continue
            if select == "files" and key not in ("filepath", "lang"):
                continue

            if isinstance(condition, str):
                sql_parts.append(f"AND {key} = ?")
                sql_params.append(condition)
            elif isinstance(condition, dict):
                for op, val in condition.items():
                    if op == "eq":
                        sql_parts.append(f"AND {key} = ?")
                        sql_params.append(val)
                    elif op == "like":
                        sql_parts.append(f"AND {key} LIKE ?")
                        sql_params.append(val)
                    elif op == "contains":
                        sql_parts.append(f"AND {key} LIKE ?")
                        sql_params.append(f"%{val}%")
                    elif op == "in":
                        placeholders = ",".join(["?" for _ in val])
                        sql_parts.append(f"AND {key} IN ({placeholders})")
                        sql_params.extend(val)

        sql_parts.append(f"LIMIT {limit} OFFSET {offset}")

        final_sql = " ".join(sql_parts)

        # Parse graph joins (requires Cypher translation later)
        # For Phase 3, we just return the cypher string we would run, but the query tool
        # will handle the actual execution.
        cypher_query = None
        if join_graph and select == "nodes":
            rel = join_graph.get("relationship", "CALLS")
            target_name = join_graph.get("target_name")
            if target_name:
                cypher_query = f"MATCH (n)-[:{rel}]->(target) WHERE target.name = '{target_name}' RETURN n.id AS id"

        return final_sql, cypher_query, sql_params
