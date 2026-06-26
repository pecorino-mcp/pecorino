# GitStats3 Pipeline Architecture

This document outlines the end-to-end pipeline of how source code is discovered, parsed, tagged with metrics, and mapped to the underlying DuckDB and Gorgonzola (KuzuDB) databases.

## 1. Discovery & Hash Tracking
When indexing is triggered (via `index_worker.py` or MCP tools), the pipeline scans the target repository for supported file extensions.
- **Incremental Indexing**: It calculates an MD5 hash and modification time for each file. 
- **Database Mapping**: These hashes are compared against the `files` table in DuckDB (`filepath PRIMARY KEY, content_hash, mtime, lang`). Files that haven't changed are completely skipped, drastically reducing ingestion time.

## 2. Parsing (AST Generation)
Modified files are dispatched to a thread pool for parallel parsing.
- **Tree-sitter C-Core**: `tree_sitter_parser.py` passes the source bytes to `tree-sitter` (via `tsgm.py` grammar manager) which builds a concrete syntax tree.
- **OOP Abstraction**: The `TreeSitterExtractor` recursively walks the tree-sitter AST and standardizes the language-specific nodes into universal OOP objects: `ClassDef`, `FunctionDef`, `InterfaceDef`, `AttributeDef`, and `ImportDef`.

## 3. Tagging & Metrics Extraction
The abstract OOP nodes are processed by `oopmetrics.py` and `maintainability.py`.
- **Cyclomatic Complexity**: Calculated by counting control flow branches (`if`, `while`, `for`, `switch`) within `FunctionDef` bodies.
- **OOP Metrics**: Calculates Weighted Methods per Class (WMC), Coupling Between Objects (CBO), Response for a Class (RFC), and Lack of Cohesion of Methods (LCOM).
- **Call Graphs**: Finds references like `self.foo()` or `obj.bar()` to identify edge dependencies.

## 4. Indexing (Database Ingestion)
To prevent SSD write amplification, all ingestion occurs inside a `/dev/shm` (RAM-disk) context via `ramdisk.py`.

### DuckDB Mapping (Code Search & Metadata)
The flattened definitions are mapped to the `code_nodes` table:
- **Columns**: `id`, `name`, `node_type`, `filepath`, `metrics_json`, `start_line`, `end_line`.
- **`body_text` Exclusion**: To keep the database lean (~2.6MB instead of ~14GB), the actual source code is NOT stored in the database. Instead, the `index.py` layer provides a `_lazy_load_body()` helper that fetches the raw text directly from the file system at query time using the `start_line` and `end_line`.
- **Full-Text Search**: DuckDB's FTS extension builds an index over the `id` and `name` columns.

### Gorgonzola Mapping (Graph & Relationships)
The relationships are mapped to a KuzuDB graph (wrapped by `gorgonzola_graph.py`). To avoid transaction overhead, nodes and edges are written to temporary CSV files in the ramdisk and ingested via Kuzu's `COPY` bulk loader.
- **Nodes**: `File`, `Class`, `Method`, `Function`, `Interface`, `Symbol`, `Module`.
- **Edges**: 
  - `CONTAINS` (File → Class, Class → Method, File → Function)
  - `CALLS` (Method → Method, Function → Function)
  - `DEPENDS_ON` (File → File, File → Module)
  - `EXTENDS` / `IMPLEMENTS` (Class → Class, Class → Interface)

## 5. Persistent Sync & Querying
Upon successful ingestion in RAM, the `.duckdb` file and `_gorgonzola` directory are flushed to `~/.gitstats3/indexes/<repo_hash>_...` in a single sequential SSD write.
When the `browse` MCP tool queries the data (e.g., `view='classes'`), it utilizes connection caching in `core.py` to maintain a persistent, read-only lock on the database, allowing sub-50ms retrieval of indexed files and their associated OOP metrics.
