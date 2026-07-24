"""Tantivy-based BM25F search index for Pecorino.

Provides per-field BM25 boosting with independent IDF per field.
Scores all fields in a single index traversal (~5ms at 10K docs).

DuckDB FTS remains as fallback when the Tantivy index is unavailable.
"""
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TantivyIndex:
    """Per-field BM25F search index using Tantivy.

    Schema mirrors the code_nodes table but optimised for text search:
    - ``name``: symbol name (boost 5.0)
    - ``kind``: node kind – function, class, method, etc. (boost 4.0)
    - ``summary``: HCGS summary text (boost 3.0)
    - ``filepath``: basename of the file path (boost 2.0)
    - ``body``: source code body text (boost 1.0)

    Boost weights are configurable at query time via
    ``PECORINO_TANTIVY_FIELD_BOOSTS`` without requiring a re-index.
    """

    # Searchable text fields
    FIELDS = ("name", "kind", "filepath", "summary", "body")

    def __init__(self, index_path: str | None = None):
        self._index = None
        self._schema = None
        self._index_path = index_path
        self._ready = False

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def build(self, nodes: list[dict[str, Any]], index_path: str | None = None) -> int:
        """Build (or rebuild) the Tantivy index from a list of code_node dicts.

        Parameters
        ----------
        nodes:
            List of dicts with keys: id, name, kind, filepath,
            hcgs_summary (optional), body_text (optional).
        index_path:
            Directory to store the Tantivy index segments.

        Returns
        -------
        int – number of documents indexed.
        """
        try:
            import tantivy
        except ImportError:
            logger.warning("tantivy package not installed – skipping Tantivy index build")
            return 0

        path = index_path or self._index_path
        if not path:
            raise ValueError("index_path must be provided")

        # Ensure clean directory
        path_obj = Path(path)
        if path_obj.exists():
            shutil.rmtree(path_obj, ignore_errors=True)
        path_obj.mkdir(parents=True, exist_ok=True)

        # Build schema
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("id", stored=True)
        for field in self.FIELDS:
            builder.add_text_field(field, stored=False)
        schema = builder.build()

        # Create index and writer
        index = tantivy.Index(schema, path=str(path_obj))
        writer = index.writer()

        count = 0
        for node in nodes:
            node_id = node.get("id", "")
            if not node_id:
                continue

            doc = tantivy.Document(
                id=node_id,
                name=node.get("name", "") or "",
                kind=node.get("kind", "") or "",
                filepath=os.path.basename(node.get("filepath", "") or ""),
                summary=node.get("hcgs_summary", "") or "",
                body=node.get("body_text", "") or "",
            )
            writer.add_document(doc)
            count += 1

        writer.commit()
        writer.wait_merging_threads()
        index.reload()

        # Store references for search
        self._index = index
        self._schema = schema
        self._index_path = str(path_obj)
        self._ready = True

        logger.info("Tantivy index built: %d documents at %s", count, path_obj)
        return count

    def open(self, index_path: str | None = None) -> bool:
        """Open an existing Tantivy index for reading.

        Returns True if the index was opened successfully.
        """
        try:
            import tantivy
        except ImportError:
            logger.debug("tantivy package not installed")
            return False

        path = index_path or self._index_path
        if not path or not Path(path).exists():
            return False

        try:
            # Rebuild schema (must match what was used during build)
            builder = tantivy.SchemaBuilder()
            builder.add_text_field("id", stored=True)
            for field in self.FIELDS:
                builder.add_text_field(field, stored=False)
            schema = builder.build()

            index = tantivy.Index(schema, path=str(path))
            index.reload()

            self._index = index
            self._schema = schema
            self._index_path = str(path)
            self._ready = True
            return True
        except Exception as e:
            logger.warning("Failed to open Tantivy index at %s: %s", path, e)
            return False

    @property
    def is_ready(self) -> bool:
        """True if the index is open and available for search."""
        return self._ready and self._index is not None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 100,
        field_boosts: dict[str, float] | None = None,
    ) -> list[tuple[str, float]]:
        """BM25F search with per-field boosting.

        Parameters
        ----------
        query:
            Natural language or keyword query string.
        limit:
            Maximum number of results.
        field_boosts:
            Optional override for per-field boost weights.
            Defaults to ``settings.tantivy_field_boosts``.

        Returns
        -------
        List of (node_id, score) tuples, sorted by descending score.
        """
        if not self.is_ready or self._index is None:
            return []

        index = self._index
        from src.mcp_server.config import settings

        boosts = field_boosts or settings.tantivy_field_boosts

        try:
            import tantivy

            searcher = index.searcher()

            # Build a boosted boolean query: one sub-query per field,
            # each boosted by its weight. This is true BM25F — each field
            # has independent IDF and the scores are combined with weights.
            sub_queries = []
            for field_name in self.FIELDS:
                boost = boosts.get(field_name, 1.0)
                if boost <= 0:
                    continue
                try:
                    field_query = index.parse_query(query, [field_name])
                    boosted = tantivy.Query.boost_query(field_query, float(boost))
                    sub_queries.append((tantivy.Occur.Should, boosted))
                except Exception:
                    pass  # Query may not match this field's analyzer

            if not sub_queries:
                return []

            combined = tantivy.Query.boolean_query(sub_queries)
            raw_results = searcher.search(combined, limit)

            results = []
            for score, doc_address in raw_results.hits:
                doc = searcher.doc(doc_address)
                node_id = doc.get_first("id")
                if node_id:
                    results.append((str(node_id), float(score)))

            return results
        except Exception as e:
            logger.warning("Tantivy search failed: %s", e)
            return []

    def close(self):
        """Release Tantivy resources."""
        self._index = None
        self._schema = None
        self._ready = False


def get_tantivy_path_for_repo(duckdb_path: str) -> str:
    """Derive the Tantivy index directory path from the DuckDB path."""
    p = Path(duckdb_path)
    tantivy_dir_name = p.stem.replace("_code_search", "_tantivy")
    return str(p.parent / tantivy_dir_name)
