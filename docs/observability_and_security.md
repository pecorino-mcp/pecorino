# Observability & Security

Pecorino is an MCP server that processes and indexes codebases. This document covers its observability and security features.

---

## 1. Prometheus Telemetry & Metrics

Pecorino exposes Prometheus telemetry (`src/mcp_server/prometheus_metrics.py`) and fine-grained structured logging across the entire indexing, search, and graph querying pipeline.

### Core Prometheus Metrics
| Metric Name | Type | Labels | Description |
| :--- | :--- | :--- | :--- |
| `mcp_tool_calls_total` (`TOOL_CALLS`) | Counter | `tool` | Total invocations per MCP tool. |
| `mcp_tool_errors_total` (`TOOL_ERRORS`) | Counter | `tool`, `error_type` | Total errors encountered per tool and error category. |
| `mcp_tool_duration_seconds` (`TOOL_DURATION`) | Histogram | `tool` | Tool execution latency in seconds. |
| `mcp_active_sessions` (`ACTIVE_SESSIONS`) | Gauge | — | Number of active HTTP SSE sessions. |
| `mcp_fts_scan_duration_seconds` (`FTS_SCAN_DURATION`) | Histogram | — | Latency of `c_fts_uring` search scans. |
| `mcp_graph_db_size_bytes` (`GRAPH_DB_SIZE`) | Gauge | — | Disk footprint of the Gorgonzola graph database in bytes. |

### Indexing Telemetry & `IndexProfiler`
The indexing engine incorporates an `IndexProfiler` (`src/mcp_server/index_pipeline.py`) that profiles each stage of codebase ingestion:
- **AST Parsing & Tree-sitter Extraction**: Parsing multi-language source trees into AST entities.
- **Gorgonzola Graph Ingestion**: Bulk loading `CodeNode`, `Identifier`, and `File` tables and inserting 20+ relationship types.
- **`c_fts_uring` Ingestion**: Populating slotted pages, inverted index entries, and positional offsets.
- **Bottom-Up Call Graph Summaries**: Topological call-graph level processing and context aggregation.
- **Vector Embedding**: Dense vector generation with ONNX (`nomic-embed-text-v1.5`).
- **WAL Checkpointing**: Flushes write-ahead logs to persistent disk immediately (`CHECKPOINT;`).

At the conclusion of an indexing run, a profiling summary is logged:
```text
=== Indexing Performance Profile ===
AST Parsing                   : 1.45s (32.1%)
c_fts_uring Indexing          : 0.98s (21.7%)
Gorgonzola Graph Ingestion    : 0.84s (18.6%)
Vector Embeddings             : 0.65s (14.4%)
Call Graph Summaries           : 0.42s (9.3%)
WAL Checkpoint                : 0.18s (4.0%)
Total                         : 4.52s
====================================
```

---

## 2. Security Architecture & Hardening

Pecorino enforces multi-layered security controls across network transports, filesystem access, database queries, and tool execution.

### OAuth 2.1 Transport Security
When running over network-accessible Server-Sent Events (SSE) transports:
- **PKCE Authorization Flow**: Full OAuth 2.1 authentication flow securing SSE connections.
- **Bearer Token Verification**: Validates agent and client access tokens on every incoming request.
- **Session Tracking**: Active sessions are tracked and bounded to prevent connection leaks.

### Role-Based Access Control (RBAC)
- **Tool-Level Gating**: MCP tools are dynamically registered based on caller roles.
- **Admin Privilege Restriction**: The `metrics` tool (which performs resource-intensive OOP, complexity, and hotspot calculations) is strictly gated to users with the `admin` role (`role == "admin"`).

### Filesystem & Workspace Isolation
- **Path Traversal Defense (`safe_path`)**: Resolves paths against the configured repository root, blocking `../` traversal attacks and verifying file existence.
- **Symlink Cycle Guard (`find_repo_root`)**: Detects and aborts circular symlinks with explicit visited-path tracking up to a max depth of 20.
- **External Path Controls (`allow_external`)**: Fine-grained boolean flag controlling whether queries and indexing can access paths beyond the immediate workspace root.

### Read-Only Query Enforcement
- **openCypher Mutation Prevention**: The `query_graph` tool validates incoming queries using regex keyword matching against mutating Cypher operations (`CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, `DROP`). Any mutating operation is immediately rejected.
- **Database Read-Only Connections**: Query clients open read-only database connections to prevent unauthorized state alteration.

### Concurrency & Resource Protection
- **Workspace Singleton Lock**: `acquire_singleton_lock()` prevents multiple writer processes from concurrently modifying the same repository index and graph database files.
- **Concurrency Rate Limiting**: Gated by `max_concurrent_tools` (default: 3) and `tool_queue_timeout` (default: 60s) to prevent resource starvation.
- **Output Clamping**: Tool output size is guarded against LLM context exhaustion by capping code lines (`MAX_CODE_LINES = 300`) and result limits (`MAX_LIMIT = 100`).
