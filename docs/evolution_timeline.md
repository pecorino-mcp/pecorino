# Pecorino Evolution Timeline (2025–2026)

This document traces the major architectural transitions of Pecorino from early 2025 to 2026, tracking its evolution from a basic Git stats utility to a code analysis MCP server.

---

## Early 2025: Foundational Architecture & MCP Migration
- **Low-Level MCP Server Core**: Introduced directory-level indexing and the initial stdio/SSE Model Context Protocol (MCP) server endpoints (`502eda2`, `9f56c6d`).
- **Graph & FTS Engine Setup**: Replaced legacy graph layers with `Gorgonzola` (a fork of the Kùzu embedded graph engine) and introduced SQLite/DuckDB FTS5 full-text indexing (`c58702b`, `570cdc0`).
- **RAM-Disk Indexing**: Added `/dev/shm` RAM-disk staging (`ramdisk.py`) to eliminate SSD write amplification and I/O bottlenecks during initial codebase parsing (`cd78fce`).
- **Security & Transports**: Migrated to FastAPI and ASGI middleware supporting streamable HTTP/SSE and OAuth 2.1 authentication (`d5a30e8`, `3de0bad`, `bb43c3e`).

---

## Mid to Late 2025: Graph Centrality, Incremental Indexing & Ranking
- **PageRank & Community Detection**: Integrated native PageRank and Leiden community detection algorithms for graph-based structural importance ranking (`3fc7139`, `76a530b`).
- **Incremental Indexing & File Watchers**: Implemented file-system watchers and metadata-based dirty tracking (`upsert_file_hashes_bulk`) to detect modified source files and update AST nodes on-the-fly (`6ee059a`).
- **Prometheus Telemetry**: Added Prometheus monitoring endpoints (`TOOL_CALLS`, `TOOL_ERRORS`, `TOOL_DURATION`, `ACTIVE_SESSIONS`, `FTS_SCAN_DURATION`, `GRAPH_DB_SIZE`) and structured logging for observability across multi-stage indexing (`fb3014b`).
- **Tree-Sitter Parsing & Language Support**: Integrated tree-sitter AST extraction with dynamic grammar loading across multiple languages (`8d8299c`, `0f42b76`).
- **Graph Schema Simplification**: Migrated back to dedicated `File` labels (alongside `CodeNode` and `Identifier`) to accelerate cypher query execution and simplify traversals (`fbcb305`).
- **Weighted Feature Scoring & Cross-Encoder Foundation**: Introduced `ltr_ranker.py` to combine graph centrality, OOD metrics, and Git churn signals into candidate rankings, complemented by ONNX Cross-Encoder reranking (`ms-marco-MiniLM-L-12-v2`).

---

## Late 2025 to 2026: C Engine (`c_fts_uring`), LLM-Assisted Cypher & Call Graph Summaries

### 1. Replacement of Tantivy with Custom C Engine (`c_fts_uring`)
- **Complete Engine Replacement**: Tantivy was fully deprecated and replaced by `c_fts_uring` (`modules/c_fts_uring`), a dedicated high-performance C storage and retrieval engine.
- **Linux `io_uring` & Custom VFS**: Integrated kernel-level asynchronous I/O via `io_uring` and a custom VFS layer for sub-millisecond retrieval latencies.
- **ACID-Compliant Storage**: Designed 4KB slotted pages with CRC32 checksums, an 8-frame clock eviction buffer pool, and crash-safe write-ahead logging (WAL).
- **Positional Indexing & BM25 Scoring**: Added inverted index positional tracking for exact phrase search, logarithmic length quantization (constant-time length lookups), and dynamic per-field BM25 weighting (`name`, `kind`, `filepath`, `summary`, `body`).
- **SQLite Virtual Table Integration**: Exposed the engine directly to SQL queries via `CREATE VIRTUAL TABLE pecorino_ast USING fts_uring(...)` (`src/mcp_server/index_db.py`).

### 2. LLM-Assisted Cypher Generation
- **MCP IDE Sampling**: Built `src/mcp_server/llm_client.py` allowing autonomous agents to query the graph using plain English questions, translated into openCypher via IDE sampling (`ctx.create_message`).
- **Resilient Local LLM Fallback**: Added automatic fallback to local models via `litellm` (e.g., Ollama / Llama 3) with stdout stream protection when sampling is unsupported or unavailable.

### 3. Graph Engine Enhancements & OpenMP Scaling
- **Automatic Gorgonzola WAL Checkpointing**: Added automated `CHECKPOINT` execution after index pipeline completion, writing WAL logs to persistent disk immediately to eliminate cold-start delays on subsequent client connections.
- **OpenMP Multi-Threading**: Enabled OpenMP multi-threaded parallel graph execution and traversals in Gorgonzola.
- **GraphAPI Refactoring**: Streamlined internal graph abstractions, isolating low-level connection lifecycles and query caching.

### 4. Bottom-Up Call Graph Summaries
- **Summary Propagation Without LLM Calls**: `hcgs.py` uses topological call-graph levels to propagate callee summaries upward to caller functions without calling an external LLM.
- **Unified Downstream Retrieval**: Embedded call graph summaries into dense vector space and fused them into weighted scoring and Cross-Encoder reranking.

### 5. Unified 8-Tool MCP Surface & Ecosystem Tooling
- **Consolidated 8 MCP Tools**: Standardized the tool surface to 8 focused endpoints (`browse`, `search`, `update_index`, `detect_changes`, `manage_adr`, `manage_snapshot`, `query_graph`, and `metrics` [admin]).
- **13 Specialized Search Modes**: Supported comprehensive modes including `auto`, `hybrid`, `fts`, `callers`, `callees`, `impact`, `usages`, `intent`, `dsl`, `functional-analysis`, `cypher`, `community`, and `trace`.
- **Architectural Decision Records (ADRs)**: Added `manage_adr` to maintain living architecture documentation (`docs/adr/`).
- **Compressed Graph Snapshots**: Introduced `manage_snapshot` for `.tar.zst` portable snapshot import/export.
- **Agent Skill Integration**: Added the `setup_environment` agent skill (`.agents/skills/setup_environment/SKILL.md`) for automated developer and agent environment bootstrapping.
