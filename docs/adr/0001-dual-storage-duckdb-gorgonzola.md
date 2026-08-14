# 1. Dual Storage Architecture (DuckDB + Gorgonzola Graph)

Date: 2026-08-14

## Status

Accepted

## Context

Pecorino requires multi-paradigm storage for codebase intelligence:
1. Tabular metadata, full-text tokens, and dense vector embeddings for fast filtering and vector search.
2. Property graph storage for deep recursive relationships (call hierarchies, class inheritance, data flow, module dependencies).

No single embedded database handles dense vector similarity joins, full-text BM25 indexes, and openCypher graph traversals with minimal memory overhead and zero external daemon dependencies.

## Decision

Adopt a dual embedded storage architecture:
- **DuckDB**: Serves as the relational metadata store, dense vector embedding storage (`ARRAY` types with `array_cosine_distance`), and auxiliary full-text search engine.
- **Gorgonzola (Kùzu/openCypher adapter)**: Serves as the embedded property graph database storing AST entities (`File`, `CodeNode`, `Identifier`, `Variable`, `Lambda`, `ControlFlow`) and relationship edges (`CALLS`, `INHERITS`, `IMPORTS`, `CONTAINS`, `READS`, `WRITES`).

Both database files reside side-by-side in the repository cache directory (`.pecorino/`).

## Consequences & Trade-offs

### Positive
- High-speed analytical queries and vector cross-joins in DuckDB without running Postgres/pgvector or external vector DB services.
- Native openCypher query capabilities with variable-length path traversal in Gorgonzola.
- Zero external daemon requirements (pure embedded in-process execution).

### Brutal Realities & Flaws
- **Dual-write inconsistency**: There is no distributed two-phase commit (2PC) between DuckDB and Gorgonzola. If indexing crashes mid-run, graph nodes and relational records can drift out of sync, requiring explicit integrity verification routines (`_verify_index_integrity`).
- **Memory mapping crashes**: Kùzu's default 8TB virtual address space mapping causes immediate crashes on memory-restricted environments with `RLIMIT_AS` or restrictive cgroup settings. Explicit bounds (`max_db_size=2GB`, `buffer_pool_size=64MB`) and explicit connection `close()` lifecycles are mandatory.
- **Connection contention**: DuckDB and Kùzu both enforce single-writer file locking, preventing multi-process concurrent indexation without serialized connection management.
