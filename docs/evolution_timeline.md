# Pecorino Evolution Timeline (2025–2026)

This document traces the major architectural transitions of Pecorino from early 2025 to the present (2026), tracking its evolution from a basic Git stats script to an advanced AI-ready Code Analysis MCP Server.

##  Early 2025: Foundational Architecture & MCP Migration
- **Low-Level MCP Server Core**: Introduced directory-level indexing and the initial stdio/SSE Model Context Protocol (MCP) server endpoints (`502eda2`, `9f56c6d`).
- **Graph & FTS Engine Setup**: Replaced legacy graph layers with `Gorgonzola` (openCypher graph adapter) and introduced SQLite/DuckDB FTS5 full-text indexing (`c58702b`, `570cdc0`).
- **RAM-Disk Indexing**: Added `/dev/shm` RAM-disk staging (`ramdisk.py`) to dramatically reduce file I/O bottlenecks during initial codebase parsing (`cd78fce`).
- **Security & Transports**: Migrated to FastAPI and ASGI middleware supporting streamable HTTP/SSE and OAuth 2.1 authentication (`d5a30e8`, `3de0bad`, `bb43c3e`).

##  Mid to Late 2025: Graph Centrality & Incremental Capabilities
- **PageRank & Community Detection**: Integrated native PageRank and Leiden community detection algorithms for graph-based structural importance ranking (`3fc7139`, `76a530b`).
- **Incremental Indexing & File Watchers**: Implemented file-system watchers to detect modified source files and update AST nodes on-the-fly (`6ee059a`).
- **Prometheus Telemetry**: Added Prometheus monitoring endpoints and structured logging for observability across multi-stage indexing (`fb3014b`).
- **Tree-Sitter Parsing & Language Support**: Integrated tree-sitter AST extraction with dynamic grammar loading (`8d8299c`, `0f42b76`).

##  2026 to Present: AI-Powered Search & Ranking Infrastructure
- **Tantivy BM25F Engine**: Integrated `tantivy_search.py` to support true per-field BM25F scoring (`name`, `kind`, `filepath`, `summary`, `body`) with configurable field boosting (`11eab58`).
- **Cross-Encoder Reranking**: Added local ONNX-based cross-encoder (`ms-marco-MiniLM-L-12-v2`) pair-wise reranking (`cross_encoder.py`) for top search candidates (`2033c0d`).
- **Learning-to-Rank (LTR) Integration**: Built `ltr_ranker.py` to blend 20+ signals (PageRank, Betweenness Centrality, OOD metrics, and Git churn/authorship entropy) into unified ranking scores (`11eab58`).
- **Intent Router & Context Assembly**: Added `intent_router.py` for heuristic query classification and `context_assembler.py` for enriching search results with graph relationships and Git issue references (`c68f14e`, `2033c0d`).
- **Index Performance & Profiling**: Added `IndexProfiler` (`f7e58e3`), index size estimation utilities (`c29370e`), and simplified Gorgonzola graph query schemas using unified `File` labels (`fbcb305`).
