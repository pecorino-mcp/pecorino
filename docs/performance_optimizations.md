# Gitstats3 Performance Optimizations

This document details the performance bottlenecks, their root causes, and the corresponding optimizations applied to the Gitstats3 codebase indexing pipeline. These issues are organized into two primary categories: **Excessive I/O Writes** and **Multiple Database Connections for Single Tasks**.

---

## Pipeline Overview

The indexing pipeline follows this sequence:

1. **Source Files** are read and checked for changes via MD5 hashes.
2. **Tree-sitter Parsing** converts source code into an Abstract Syntax Tree (AST).
3. **AST Extraction** transforms the raw AST into Object-Oriented representations (`ClassDef`, `FunctionDef`, `InterfaceDef`, `ImportDef`).
4. **DuckDB Inserts** store the file nodes into a columnar database (search index).
5. **Gorgonzola Graph Inserts** store nodes and edges for dependency resolution.
6. **Symbol Resolution** resolves dangling import/function symbols into concrete graph edges.
7. **FTS Rebuild** re-generates the Full-Text Search index.
8. **Final SSD Sync** moves the built databases from RAM to persistent storage.

---

## Part 1: Excessive I/O Writes

### 1.1 Rebuilding FTS Index Per-File

| | |
|---|---|
| **Commit** | `7f5edbe` |
| **Problem** | The `rebuild_fts` flag in `index_file()` defaulted to `True`. Because DuckDB does not support incremental FTS updates, this meant the **entire FTS index** was dropped and rebuilt from scratch after *every single file* was indexed. For a 500-file repository, this triggered 500 full FTS rebuilds. |
| **Solution** | Defaulted `rebuild_fts=False`. Batch operations (like `index_directory()`) now index all files first and perform a **single** `rebuild_fts()` at the very end. |
| **Impact** | Eliminated ~499 redundant full-table scans and index rebuilds per batch run. |

### 1.2 Row-by-Row DuckDB Inserts

| | |
|---|---|
| **Commit** | `7f5edbe` |
| **Problem** | `index_nodes()` inserted AST nodes one by one in a Python `for` loop, issuing a separate `INSERT` statement per row. DuckDB is a columnar engine optimized for vectorized operations—row-by-row inserts force it into a worst-case transactional write path. |
| **Solution** | Gathered all nodes into a list of tuples and replaced the loop with a single `conn.executemany()` batch insert wrapped in an explicit `BEGIN TRANSACTION` / `COMMIT` block. |
| **Impact** | DuckDB write throughput improved by an order of magnitude for large file sets. |

### 1.3 Row-by-Row Gorgonzola (Kùzu) Graph Inserts

| | |
|---|---|
| **Commits** | `d8ea1bc`, `1d0f145` |
| **Problem** | Even after removing `body_text`, executing thousands of individual `CREATE` or `MERGE` Cypher queries in loops for graph relationships (`CONTAINS`, `CALLS`, `EXTENDS`, `IMPLEMENTS`, `DEPENDS_ON`) forced Gorgonzola into lock/flush cycles, bottlenecking CPU and disk I/O. Each `MERGE` query triggered a transaction open, WAL write, and commit. |
| **Solution** | Refactored `GorgonzolaGraph` to write nodes and edges to **temporary CSV files**. Once populated, it executes `COPY <Label> FROM '<path>'` for bulk insertion. This bypasses the Cypher query planner entirely and leverages Kùzu's native bulk loader. |
| **Impact** | Graph insertion time dropped from seconds to milliseconds. WAL churn eliminated. |

### 1.4 Massive SSD Write Amplification from `body_text` Storage (14 GB+ for 110k LOC)

| | |
|---|---|
| **Commits** | `1d0f145`, `6208d23` |
| **Problem** | Storing `body_text` (the full source code of every function and class) in both DuckDB and Gorgonzola triggered extreme write amplification. Because DuckDB is columnar and Gorgonzola uses LSM-trees, row-by-row insertion of large strings caused constant WAL allocations, block rewrites, and compactions. A 110k LOC repository generated over **14 GB of physical SSD writes**. |
| **Root Cause** | The codebase was treating the database as a code mirror rather than a metadata index. Storing full source text duplicated data already available on the filesystem, while also causing the database's internal storage engine to amplify writes through journaling, compaction, and columnar encoding overhead. |
| **Solution** | <ol><li>**Removed `body_text` from the database schemas.** Databases now only store `filepath`, `start_line`, and `end_line`.</li><li>**Added `_lazy_load_body()` in `index.py`.** The `browse` / `search` layer dynamically fetches the source block from the filesystem at query time using the stored line range.</li><li>**Removed `metrics_json` column** (commit `6208d23`). OOP metrics are re-computed on-the-fly rather than stored as serialized JSON blobs.</li></ol> |
| **Impact** | Physical DB size on disk dropped from ~30 MB to ~17 MB. SSD writes dropped from **14 GB to 13 MB**. |

### 1.5 SQLite Journal Size Limit (Pre-DuckDB Era)

| | |
|---|---|
| **Commit** | `1d4726a` |
| **Problem** | Before the DuckDB migration, the index was backed by SQLite. The WAL journal had a 64 MB size limit, which was insufficient for large repositories. The journal would overflow, triggering frequent checkpoint writes to the main database file. |
| **Solution** | Increased `journal_size_limit` from 64 MB to 384 MB. This was a stopgap measure—the root cause was later addressed by migrating entirely to DuckDB. |
| **Note** | This fix is now **obsolete**. Commit `0de69fe` migrated the entire storage backend to DuckDB, eliminating SQLite and its journal entirely. |

### 1.6 Ramdisk (`/dev/shm`) Indexing

| | |
|---|---|
| **Commit** | `1d0f145` |
| **Problem** | Even with bulk CSV loading and `body_text` removal, DuckDB and Gorgonzola generate temporary intermediate files (WALs, compaction logs, FTS temporary indices) during indexing. These intermediate writes totaled ~13 MB of unavoidable SSD writes per full index. On consumer SSDs with limited TBW (Total Bytes Written) endurance, frequent re-indexing posed a longevity concern. |
| **Solution** | Created `RamdiskIndex` (`ramdisk.py`) to map the workspace to `/dev/shm` (Linux tmpfs). All intermediate WAL flushing and compaction happens entirely in RAM. Only the perfectly compacted, final database files are synced sequentially to the SSD via `shutil.copy2` + `os.replace` (atomic rename) at the very end. |
| **Quota Enforcement** | The ramdisk enforces a configurable byte quota (`max_bytes`). If the in-RAM database exceeds the quota during indexing, a `RamdiskQuotaExceeded` exception is raised and the ramdisk is cleaned up without corrupting SSD data. |
| **Dynamic Allocation** | Commit `6208d23` added dynamic quota calculation: `projected_db_bytes = total_source_bytes × 40`, with a 1.5× safety buffer and a minimum of 1 GB. This prevents quota overflows for large repositories without wasting RAM for small ones. |
| **Impact** | Total SSD wear dropped to **~5 MB** per full index. Build times dropped to under 5 seconds. Indexing is now purely CPU/RAM bound and guarantees atomic updates (no corrupted DBs on crash). |

---

## Part 2: Multiple Database Connections for Single Tasks

### 2.1 Excessive DuckDB Connections (5–6 per file)

| | |
|---|---|
| **Commit** | `7f5edbe` |
| **Problem** | Every single DuckDB operation (`index_nodes`, `clear_file`, `upsert_file_hash`, `get_file_hash`, `search`) opened a **new** database connection via `with duckdb.connect()`. This caused massive connection overhead, resulting in 5–6 connection round-trips *per file*. For a 500-file repository, this was ~3,000 connection open/close cycles. Each connection open triggers schema verification, WAL replay, and lock acquisition. |
| **Solution** | Refactored `CodeSearchIndex` to instantiate a **persistent connection** (`self._conn`) upon initialization in `__init__()`. This persistent connection is reused across all queries during the lifetime of the object. A `close()` method and `__del__` destructor ensure proper cleanup. |
| **Impact** | Reduced connection overhead to exactly **2** connections per session (1 for DuckDB, 1 for Gorgonzola). |

### 2.2 Gorgonzola: New Database + Connection per Query

| | |
|---|---|
| **Commit** | `d8ea1bc` |
| **Problem** | Each call to `GorgonzolaGraph.query()`, `insert_nodes_bulk()`, or `insert_edges_bulk()` opened a brand new `gorgonzola.Database()` + `gorgonzola.Connection()` context if the graph was not already inside a `with` block. Since post-processing operations like `resolve_symbols()` and `clear_file()` executed multiple Cypher queries consecutively, each query opened and closed its own database handle. This was especially wasteful because Gorgonzola (Kùzu) must replay the WAL and verify the schema on every `Database()` open. |
| **Solution** | <ol><li>**Context manager support** (`__enter__` / `__exit__`): `GorgonzolaGraph` now supports `with self.graph:` blocks that hold a single persistent `Database` + `Connection` pair for the duration of a batch operation.</li><li>**`query_batch()` method**: Accepts a list of Cypher queries and executes them all within a **single** connection context, instead of opening a new context per query.</li><li>**Dual-mode `_in_context` flag**: Methods like `query()`, `insert_nodes_bulk()`, and `insert_edges_bulk()` check `self._in_context`. If the caller is inside a `with` block, they reuse the existing connection. If not, they fall back to opening a temporary one (backwards-compatible).</li></ol> |
| **Impact** | Eliminated hundreds of redundant database opens during indexing. Symbol resolution (`resolve_symbols()`) dropped from 7 separate connection contexts to 1. |

### 2.3 Redundant Gorgonzola Label Lookups (7 queries per edge)

| | |
|---|---|
| **Commit** | `7f5edbe`, `d8ea1bc` |
| **Problem** | In `GorgonzolaGraph.insert_edges_bulk()`, the code needed to know the label of the source and destination nodes (e.g., `File`, `Class`, `Method`) to construct the CSV `COPY FROM` command with correct `FROM` / `TO` clauses. It called `_get_node_label()`, which executed up to **7 separate Cypher queries** (one for each node table: `File`, `Class`, `Method`, `Function`, `Interface`, `Symbol`, `Module`) to find the correct label. For 1,000 edges, this was up to 14,000 queries. |
| **Solution** | The label for each node is already known at insertion time during `insert_nodes_bulk()`. Modified `insert_nodes_bulk()` to return a `{node_id: label}` **id_map** dictionary. This map is passed directly to `insert_edges_bulk()` to serve as an in-memory cache, skipping the expensive `_get_node_label()` fallback almost entirely. The label cache (`self._label_cache`) persists across calls for any remaining edge lookups. |
| **Impact** | Edge insertion queries dropped from **O(7n)** to **O(1)** per edge. |

### 2.4 MCP SDK Latency: New Connections on Every Tool Call (240 ms overhead)

| | |
|---|---|
| **Commits** | `1d0f145`, `b8e0399` |
| **Problem** | The `do_browse`, `search_code`, and `get_metrics` tool handlers in `core.py` instantiated a **brand new** `CodeSearchIndex` and `GraphAPI` on every single MCP tool invocation. Each `CodeSearchIndex.__init__()` calls `duckdb.connect()` (which verifies the schema and runs `migrate_codebase()`), and `GorgonzolaGraph.__init__()` opens a Gorgonzola database to verify the graph schema. This added **240 ms+ of synchronous latency** before any search logic even ran. |
| **Solution** | Implemented a global `_API_CACHE` dictionary in `core.py`. Tool handlers now call `_get_cached_api(repo_root, db_path, api_type)`, which returns persistent, `read_only=True` connections to DuckDB and Gorgonzola. Connections are created once and reused for all subsequent queries to the same repository. |
| **Impact** | Cold-start latency is unchanged (~240 ms for first query). Warm query latency dropped from **~240 ms** to **~22 ms**, satisfying sub-100ms UI requirements. |

---

## Part 3: Other Performance Optimizations

### 3.1 Tree-sitter Parser Reallocation

| | |
|---|---|
| **Commit** | `7f5edbe` |
| **Problem** | `parse_with_tree_sitter()` instantiated a new `TreeSitterGrammarManager` and a new `tree_sitter.Parser` object on every single call. Because the manager's cache was instance-level, the grammar was re-loaded from disk repeatedly. |
| **Solution** | Implemented module-level singletons (`_grammar_manager` and `_parsers` dict). Parsers are now lazily loaded and cached per language. Only **1** parser instance is created per language during an entire batch indexing run. |

### 3.2 Multi-Threaded File Parsing

| | |
|---|---|
| **Commit** | `383c540` |
| **Problem** | Files were parsed sequentially, one at a time. AST parsing is CPU-bound, so this left multiple cores idle. |
| **Solution** | Introduced `ThreadPoolExecutor` with `max_workers = min(8, os.cpu_count())` for parallel AST parsing. File read → hash → parse tasks are dispatched concurrently. Results are aggregated before bulk database writes. |

### 3.3 Storage Backend Migration (SQLite → DuckDB)

| | |
|---|---|
| **Commit** | `0de69fe` |
| **Problem** | The original SQLite-backed index required extensive pragma tuning (`journal_size_limit`, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`) and still suffered from poor FTS performance and write amplification. |
| **Solution** | Complete migration to DuckDB, which provides native columnar storage, built-in FTS extension, and vectorized batch insert support (`executemany`). |

### 3.4 Graph Backend Migration (graphqlite → Gorgonzola/Kùzu)

| | |
|---|---|
| **Commit** | `e9176b8` |
| **Problem** | The original `graphqlite` backend lacked bulk import capabilities and had limited query performance. |
| **Solution** | Replaced `graphqlite` with Gorgonzola (a Kùzu-based graph database wrapper). Gorgonzola supports `COPY FROM` bulk CSV loading, Cypher queries, and native graph traversal (`RECURSIVE_REL`). |
| **Note** | `RECURSIVE_REL` is available in the Gorgonzola API but is **not currently used** by gitstats3. Variable-length path traversals (e.g., transitive impact analysis) use explicit `*1..N` depth syntax in Cypher instead. Global dependency scoring uses `networkx.pagerank()` rather than the graph engine's native recursive relationships. |

---

## Timeline

| Commit | Date (relative) | Summary |
|--------|-----------------|---------|
| `26f37b6` | Early | SQLite FTS5 migration, database performance pragmas |
| `1d4726a` | Early | SQLite journal_size_limit bump (64 MB → 384 MB) |
| `e9176b8` | Mid | Replace graphqlite with Gorgonzola (Kùzu) |
| `0de69fe` | Mid | Migrate storage from SQLite to DuckDB |
| `7f5edbe` | Mid | Parser caching, batch execution, persistent DuckDB connections |
| `383c540` | Mid | Multi-threaded file parsing |
| `d8ea1bc` | Mid–Late | GorgonzolaGraph connection optimization, context managers |
| `b8e0399` | Mid–Late | Performance optimization documentation |
| `1d0f145` | Late | Ramdisk indexing, `body_text` removal, `_API_CACHE` |
| `6208d23` | Latest | Remove `metrics_json`, dynamic RAM disk allocation |

---

## Current State Assessment

### Resolved
- **SSD write amplification**: Reduced from 14 GB to ~5 MB per full index via `body_text` removal + ramdisk.
- **DuckDB connection churn**: Single persistent connection per `CodeSearchIndex` instance.
- **Gorgonzola connection churn**: Context manager + `query_batch()` reduces connections to 1 per batch.
- **MCP query latency**: `_API_CACHE` provides persistent read-only connections (~22 ms warm latency).
- **FTS rebuild overhead**: Single rebuild at end of batch, not per-file.

### Remaining Considerations
1. **Gorgonzola fallback connections**: When `GorgonzolaGraph` methods are called *outside* a `with` block (i.e., `_in_context` is `False`), they still open a temporary `Database + Connection` per call. This is by design for backwards compatibility, but callers should prefer the context manager pattern.
2. **`upsert_file_hash()` individual MERGE**: The single-file `upsert_file_hash()` method in `index.py` still issues a standalone `self.graph.query()` (MERGE) call per file. The batch path (`upsert_file_hashes_bulk`) only updates DuckDB—it does not batch-update Gorgonzola `File` nodes. This is acceptable because `upsert_file_hash()` is only called in the single-file `index_file()` code path, not the batch `index_directory()` path.
3. **`_API_CACHE` has no eviction**: The global cache in `core.py` grows unbounded if the server processes queries for many different repositories. For a typical single-repo deployment this is not an issue.
4. **`RECURSIVE_REL` unused**: Gorgonzola's native recursive relationship type is not leveraged. Impact analysis uses explicit Cypher `*1..N` depth syntax, and global scoring uses `networkx.pagerank()` via a separate in-memory graph. Migrating to native recursive traversal could eliminate the `networkx` dependency and reduce memory usage for large graphs.
