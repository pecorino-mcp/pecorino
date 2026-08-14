# 3. Hybrid Search: BM25F, Dense Embeddings, RRF, and Cross-Encoder Reranking

Date: 2026-08-14

## Status

Accepted

## Context

Code search queries span two distinct modalities:
1. Exact identifier lookup (e.g., `resolve_definition`, `CodeSearchIndex`, variable names).
2. Semantic / natural language discovery (e.g., "where do we handle session tokens", "authentication middleware").

Pure keyword search fails on synonyms and conceptual questions. Pure vector search fails on precise camelCase/snake_case identifier matching and exact substring queries.

## Decision

Implement a multi-stage hybrid search and ranking pipeline:
1. **Full-Text Search**: Use Tantivy BM25F with per-field scoring (`name`, `kind`, `filepath`, `summary`, `body_text`) and fallback to DuckDB FTS.
2. **Dense Vector Search**: Generate local 384-dim embeddings (`BAAI/bge-small-en-v1.5` / `nomic-embed-text`) via ONNX Runtime and compute cosine distance in DuckDB.
3. **Reciprocal Rank Fusion (RRF)**: Merge ranked result sets using mathematical rank inverse weighting ($1 / (k + \text{rank})$) scaled by structural PageRank and community importance.
4. **Learning-to-Rank (LTR) & Cross-Encoder (CE)**: Extract 20+ static/graph/git features for candidates, sort by XGBoost/linear ranker, and rerank top-N results through an ONNX Cross-Encoder (`ms-marco-MiniLM-L-12-v2`).

## Consequences & Trade-offs

### Positive
- Exceptional retrieval quality: high precision for exact identifier lookups combined with deep semantic discovery for natural language questions.
- Low online latency: Tantivy delivers sub-millisecond BM25F queries, and Cross-Encoder runs only on top-N candidates.

### Brutal Realities & Flaws
- **Heavy Local Dependencies**: Bundling ONNX Runtime, HuggingFace tokenizers, and Tantivy native binaries significantly inflates deployment complexity and runtime memory footprint.
- **CPU Resource Contention**: Computing vector embeddings during full-repo indexing saturates CPU cores. Without thread limits (`OMP_NUM_THREADS=4`), embedding generation starves concurrent server threads.
- **Model Drift & Fallbacks**: If ONNX models fail to load or native libraries fail on specific architectures, the system degrades silently to DuckDB FTS-only mode without semantic ranking.
