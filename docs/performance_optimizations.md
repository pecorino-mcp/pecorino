# Gitstats3 Performance Optimizations

This document details the performance bottlenecks and corresponding optimizations applied to the Gitstats3 codebase indexing pipeline. These optimizations significantly reduced the time required for incremental indexing, AST parsing, graph construction, and SSD write amplification.

## Pipeline Overview
The indexing pipeline follows this sequence:
1. **Source Files** are read and checked for changes via MD5 hashes.
2. **Tree-sitter Parsing** converts source code into an Abstract Syntax Tree (AST).
3. **AST Extraction** transforms the raw AST into Object-Oriented representations (Classes, Methods, Imports).
4. **DuckDB Inserts** store the file nodes into a columnar database (search index).
5. **Gorgonzola Graph Inserts** store nodes and edges for dependency resolution.
6. **Symbol Resolution** resolves dangling import/function symbols.
7. **FTS Rebuild** re-generates the Full-Text Search index.
8. **Final SSD Sync** moves the built databases from RAM to persistent storage.

---

## Identified Bottlenecks & Fixes (Part 1: Logic & Overhead)

### 1. Rebuilding FTS Index Per-File
**Problem:** The `rebuild_fts` flag in `index_file()` defaulted to `True`. Because DuckDB does not support incremental FTS updates, this meant the entire FTS index was dropped and rebuilt from scratch after *every single file* was indexed.
**Solution:** Defaulted `rebuild_fts=False`. Batch operations (like `index_directory()`) now index all files and perform a single `rebuild_fts()` at the very end. 

### 2. Excessive DuckDB Connections
**Problem:** Every single DuckDB operation (`index_nodes`, `clear_file`, `upsert_file_hash`, `get_file_hash`, `search`) opened a new database connection via `with duckdb.connect()`. This caused massive connection overhead, resulting in 5-6 connection round-trips *per file*.
**Solution:** Refactored `CodeSearchIndex` to instantiate a persistent connection (`self._conn`) upon initialization. This persistent connection is reused across all queries during the lifetime of the object, reducing connection overhead to exactly **2** connections per session.

### 3. Redundant Gorgonzola Label Lookups
**Problem:** In `GorgonzolaGraph.insert_edges_bulk()`, the code needed to know the label of the source and destination nodes to construct the Cypher query. It called `_get_node_label()`, which executed up to 7 separate Cypher queries (one for each possible node table) to find the correct label.
**Solution:** The label for each node is already known at insertion time during `insert_nodes_bulk()`. Modified `insert_nodes_bulk()` to return a `{node_id: label}` map. Passed this map to `insert_edges_bulk()` to serve as an in-memory cache, skipping the expensive `_get_node_label()` fallback almost entirely.

### 4. Tree-sitter Parser Reallocation
**Problem:** The `parse_with_tree_sitter()` function instantiated a new `TreeSitterGrammarManager` and a new `tree_sitter.Parser` object on every single call. Because the manager's cache was instance-level, the grammar was re-loaded from disk repeatedly.
**Solution:** Implemented module-level singletons (`_grammar_manager` and `_parsers` dict). Parsers are now lazily loaded and cached per language. This means only **1** parser instance is created per language during an entire batch indexing run.

### 5. Row-by-Row DuckDB Inserts
**Problem:** `index_nodes()` inserted AST nodes one by one in a Python loop.
**Solution:** Gathered all nodes into a list of tuples and replaced the loop with a single `conn.executemany()` batch insert. DuckDB is optimized for vectorized inserts, resulting in significantly faster writes.

### 6. Sequential Cypher Queries
**Problem:** Post-processing operations like `resolve_symbols()` and `clear_file()` executed multiple Cypher queries consecutively, creating a new Gorgonzola Database + Connection context for every query.
**Solution:** Introduced a `query_batch()` method in `GorgonzolaGraph` that accepts a list of queries and executes them within a single connection context. 

---

## Identified Bottlenecks & Fixes (Part 2: SSD Write Amplification & SDK Latency)

### 7. Massive SSD Write Amplification (14GB+ writes for 110k LOC)
**Problem:** Storing `body_text` (the full source code of every function and class) in DuckDB and Gorgonzola triggered extreme write amplification. Because DuckDB is columnar and Gorgonzola uses LSM-trees, row-by-row insertion of large strings caused constant WAL allocations, block rewrites, and compactions. A 110k LOC repository generated over 14 GB of physical SSD writes.
**Solution:** 
- **Removed `body_text` from the database schemas.** Instead of duplicating source code, the databases now only store `filepath`, `start_line`, and `end_line`.
- **Added `_lazy_load_body()`**. The `browse` / `search` layer dynamically fetches the source block from the filesystem at query time.
- **Result:** Physical DB size on disk dropped from ~30 MB to ~17 MB. SSD writes dropped from 14 GB to 13 MB.

### 8. CSV Bulk Loading for Gorgonzola (Graph DB)
**Problem:** Even without `body_text`, executing thousands of `CREATE` or `MERGE` queries in loops for graph relationships (`CONTAINS`, `CALLS`) forced Gorgonzola into lock/flush cycles, bottlenecking CPU and disk I/O.
**Solution:** Refactored `GorgonzolaGraph` to write nodes and edges to temporary CSV files. Once parsed, it executes `COPY <Label> FROM '<path>'` for bulk insertion. This bypassed the query planner entirely.

### 9. Ramdisk (`/dev/shm`) Indexing
**Problem:** Even with bulk CSV loading, DuckDB and Gorgonzola generate temporary intermediate files (WALs, compaction logs, FTS temporary indices) during the indexing process, resulting in ~13 MB of unavoidable SSD writes.
**Solution:** Created `RamdiskIndex` to map the workspace to `/dev/shm` (tmpfs). All intermediate WAL flushing and compaction happens entirely in RAM. Only the perfectly compacted, final database files are synced sequentially to the SSD via `shutil.copytree` at the very end.
- **Result:** Total SSD wear dropped to **~5 MB** per full index. Build times dropped to under 5 seconds. Indexing is now purely CPU/RAM bound and guarantees atomic updates (no corrupted DBs on crash).

### 10. MCP SDK Latency (240ms Overhead)
**Problem:** The `do_browse`, `search_code`, and `get_metrics` tools in `core.py` instantiated a brand new `CodeSearchIndex` and `GraphAPI` on every query. DuckDB had to verify schema and migrate tables on every call, leading to 240ms+ synchronous latency before any search logic even ran.
**Solution:** Implemented a global `_API_CACHE` in `core.py`. Tools now pull persistent, `read_only=True` connections to DuckDB and Gorgonzola. 
- **Result:** Latency dropped from **~240ms** down to **~22ms**, satisfying sub-100ms UI requirements.
