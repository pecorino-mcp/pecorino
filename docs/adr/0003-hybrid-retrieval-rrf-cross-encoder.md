# 3. Hybrid Search: c-fts-uring, Dense Embeddings, RRF, and Cross-Encoder Reranking

Date: 2026-08-14

## Status

Accepted (Supersedes prior Tantivy/DuckDB implementation)

## Context

Code search queries span two distinct modalities:
1. Exact identifier lookup.
2. Semantic / natural language discovery.

Previously, Tantivy and DuckDB were used. However, managing Tantivy Rust bindings alongside DuckDB processes introduced severe IPC overhead and cold-start latency.

## Decision

Implement a multi-stage hybrid search and ranking pipeline native to SQLite3:
1. **Full-Text Search**: Use `c-fts-uring`, a highly optimized C extension for SQLite3 leveraging Linux `io_uring` for async I/O. It replaces Tantivy and standard SQLite FTS5 for maximum concurrent text retrieval.
2. **Dense Vector Search**: Generate local 384-dim embeddings (`BAAI/bge-small-en-v1.5`) via ONNX Runtime and compute cosine distance natively in SQLite3 using an HNSW (Hierarchical Navigable Small World) extension (`code_vss_idx`).
3. **Reciprocal Rank Fusion (RRF)**: Merge ranked result sets mathematically, scaled by structural PageRank and community importance.
4. **Learning-to-Rank (LTR) & Cross-Encoder (CE)**: Extract 20+ static/graph/git features for candidates, sort by XGBoost/linear ranker, and rerank top-N results through an ONNX Cross-Encoder (`ms-marco-MiniLM-L-12-v2`).

## Consequences & Trade-offs

### Positive
- Unified query planner: both semantic HNSW vector searches and `c-fts-uring` queries are executed entirely within a single SQLite3 connection, eliminating inter-process IPC overhead.
- Linux `io_uring` provides exceptional I/O scalability during concurrent search requests without saturating thread pools.

### Brutal Realities & Flaws
- **Kernel Dependency**: `c-fts-uring` strictly binds the architecture to modern Linux kernels. It cannot run on macOS or Windows without virtualization.
- **Silent Fallbacks**: If the custom C extensions (`fts_uring.so` or HNSW) fail to load (common during local dev environment mismatches), the system degrades silently or throws operational errors instead of falling back to standard SQLite FTS5.
- **Documentation Discrepancy**: Older design docs still reference Tantivy and DuckDB, creating severe cognitive dissonance for maintainers.
