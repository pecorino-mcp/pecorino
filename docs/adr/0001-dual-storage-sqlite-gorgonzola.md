# 1. Dual Storage Architecture (SQLite3 + Gorgonzola Graph)

Date: 2026-08-14

## Status

Accepted (Supersedes prior DuckDB implementation)

## Context

Pecorino requires multi-paradigm storage for codebase intelligence:
1. Tabular metadata, full-text tokens, and dense vector embeddings for fast filtering and vector search.
2. Property graph storage for deep recursive relationships (call hierarchies, class inheritance, data flow, module dependencies).

Originally, DuckDB was used for relational/vector data. However, DuckDB proved too heavy and had locking/concurrency issues during incremental indexing.

## Decision

Migrate to a dual embedded storage architecture utilizing SQLite3 and Gorgonzola:
- **SQLite3**: Serves as the relational metadata store. It utilizes a custom HNSW vector extension for `ARRAY` embedding similarity and an io_uring-backed custom C-extension (`c-fts-uring`) for high-performance async full-text search.
- **Gorgonzola (Kùzu/openCypher adapter)**: Serves as the embedded property graph database storing AST entities (`File`, `CodeNode`, `Identifier`, etc.) and relationship edges (`CALLS`, `INHERITS`, etc.).

Both database files reside side-by-side in the repository cache directory (`.pecorino/`). 

## Consequences & Trade-offs

### Positive
- SQLite3 is significantly more stable, ubiquitous, and lightweight for transactional metadata updates than DuckDB.
- Custom `c-fts-uring` bypasses standard SQLite FTS5 CPU overhead, using io_uring for zero-copy async disk reads during search.
- Native openCypher query capabilities with variable-length path traversal remain available in Gorgonzola.

### Brutal Realities & Flaws
- **Stale Documentation**: Existing documentation (`docs/search_architecture.md`, `docs/evolution_timeline.md`) still falsely claims DuckDB and Tantivy are used. The architecture evolved faster than the documentation.
- **Dual-write inconsistency**: There is still no distributed two-phase commit (2PC) between SQLite and Gorgonzola. Partial indexing crashes lead to orphaned nodes or dead edges.
- **Custom Extension Hell**: Compiling and linking custom SQLite extensions (`fts_uring.so` and HNSW/VSS) creates massive deployment friction on systems without modern Linux kernels (io_uring requires kernel 5.1+) or specific C compilers.
