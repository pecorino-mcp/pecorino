# Gitstats3 Performance Optimizations

This document details the performance bottlenecks and corresponding optimizations applied to the Gitstats3 codebase indexing pipeline. These optimizations significantly reduced the time required for incremental indexing, AST parsing, and graph construction.

## Pipeline Overview
The indexing pipeline follows this sequence:
1. **Source Files** are read and checked for changes.
2. **Tree-sitter Parsing** converts source code into an Abstract Syntax Tree (AST).
3. **AST Extraction** transforms the raw AST into Object-Oriented representations (Classes, Methods, Imports).
4. **DuckDB Inserts** store the file nodes into a columnar database.
5. **Gorgonzola Graph Inserts** store nodes and edges for dependency resolution.
6. **Symbol Resolution** resolves dangling import/function symbols.
7. **FTS Rebuild** re-generates the Full-Text Search index.

---

## Identified Bottlenecks & Fixes

### 1. Rebuilding FTS Index Per-File
**Problem:** The `rebuild_fts` flag in `index_file()` defaulted to `True`. Because DuckDB does not support incremental FTS updates, this meant the entire FTS index was dropped and rebuilt from scratch after *every single file* was indexed.
**Solution:** Defaulted `rebuild_fts=False`. Batch operations (like `index_directory()`) now index all files and perform a single `rebuild_fts()` at the very end. The single-file indexing path was explicitly updated to pass `rebuild_fts=True` to maintain expected behavior.

### 2. Excessive DuckDB Connections
**Problem:** Every single DuckDB operation (`index_nodes`, `clear_file`, `upsert_file_hash`, `get_file_hash`, `search`) opened a new database connection via `with duckdb.connect()`. This caused massive connection overhead, resulting in 5-6 connection round-trips *per file*.
**Solution:** Refactored `CodeSearchIndex` to instantiate a persistent connection (`self._conn`) upon initialization. This persistent connection is reused across all queries during the lifetime of the object, reducing connection overhead to exactly **2** connections per session (1 persistent, 1 short-lived for schema migrations).

### 3. Redundant Gorgonzola Label Lookups
**Problem:** In `GorgonzolaGraph.insert_edges_bulk()`, the code needed to know the label of the source and destination nodes to construct the Cypher query. It called `_get_node_label()`, which executed up to 7 separate Cypher queries (one for each possible node table) to find the correct label. This resulted in up to 14 queries per edge inserted.
**Solution:** The label for each node is already known at insertion time during `insert_nodes_bulk()`. Modified `insert_nodes_bulk()` to return a `{node_id: label}` map. Passed this map to `insert_edges_bulk()` to serve as an in-memory cache, skipping the expensive `_get_node_label()` fallback almost entirely.

### 4. Tree-sitter Parser Reallocation
**Problem:** The `parse_with_tree_sitter()` function instantiated a new `TreeSitterGrammarManager` and a new `tree_sitter.Parser` object on every single call. Because the manager's cache was instance-level, the grammar was re-loaded from disk repeatedly.
**Solution:** Implemented module-level singletons (`_grammar_manager` and `_parsers` dict). Parsers are now lazily loaded and cached per language. This means only **1** parser instance is created per language during an entire batch indexing run.

### 5. Row-by-Row DuckDB Inserts
**Problem:** `index_nodes()` inserted AST nodes one by one in a Python loop.
**Solution:** Gathered all nodes into a list of tuples and replaced the loop with a single `conn.executemany()` batch insert. DuckDB is optimized for vectorized inserts, resulting in significantly faster writes.

### 6. Sequential Cypher Queries
**Problem:** Post-processing operations like `resolve_symbols()` and `clear_file()` executed multiple Cypher queries consecutively, creating a new Gorgonzola Database + Connection context for every query.
**Solution:** Introduced a `query_batch()` method in `GorgonzolaGraph` that accepts a list of queries and executes them within a single connection context. This reduced Gorgonzola connection counts dramatically (e.g., from ~215 down to 120 for a typical 30-file batch).

---

## Before vs. After Summary (Example: 39 Files)

| Metric | Before Optimizations | After Optimizations |
| :--- | :--- | :--- |
| **Execution Time** | Highly likely to time out / multiple minutes | ~95 seconds |
| **DuckDB Connections** | ~150+ | 2 |
| **Gorgonzola Connections** | ~215 | 120 |
| **FTS Rebuilds** | 29 | 1 |
| **Tree-sitter Parsers** | 29 | 1 |
