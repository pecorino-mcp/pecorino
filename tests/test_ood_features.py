"""Tests for Phase 3: Object-Oriented Design (OOD) Features."""
from src.mcp_server.index_db import CodeSearchIndex


class TestOODFeatures:
    """Unit tests for Phase 3 OOD Features computation & persistence."""

    def test_update_ood_features_bulk(self, tmp_path):
        """Verify bulk update of OOD feature columns in DuckDB code_nodes."""
        db_path = str(tmp_path / "test_ood.duckdb")
        search_index = CodeSearchIndex(db_path=db_path)

        # Insert sample code_nodes
        node_id = "src/service.py::OrderService::10"
        search_index.index_nodes([
            {
                "id": node_id,
                "name": "OrderService",
                "kind": "class",
                "filepath": "src/service.py",
                "start_line": 10,
                "end_line": 50,
            }
        ])

        # Apply OOD features bulk update
        ood_feats = [
            {
                "id": node_id,
                "instability": 0.25,
                "coupling": 8.0,
                "depth": 2,
                "inheritance_depth": 1,
                "betweenness": 0.045,
            }
        ]
        search_index.update_ood_features_bulk(ood_feats)

        # Retrieve and verify from DuckDB
        row = search_index._conn.execute("""
            SELECT instability, coupling, depth, inheritance_depth, betweenness
            FROM code_nodes
            WHERE id = ?
        """, (node_id,)).fetchone()

        assert row is not None
        assert abs(row[0] - 0.25) < 0.001
        assert abs(row[1] - 8.0) < 0.001
        assert row[2] == 2
        assert row[3] == 1
        assert abs(row[4] - 0.045) < 0.0001

        search_index.close()

    def test_instability_and_coupling_calculation(self):
        """Verify instability formula: out_degree / (in_degree + out_degree)."""
        in_deg, out_deg = 3, 9
        coupling = float(in_deg + out_deg)
        instability = out_deg / coupling if coupling > 0 else 0.0

        assert coupling == 12.0
        assert abs(instability - 0.75) < 0.001

        # Zero coupling edge case
        in_deg, out_deg = 0, 0
        coupling = float(in_deg + out_deg)
        instability = out_deg / coupling if coupling > 0 else 0.0

        assert coupling == 0.0
        assert instability == 0.0
