# Pecorino Architecture Overview

Pecorino is an MCP (Model Context Protocol) server designed for deep code analysis, search, and repository analytics. The system operates on a dual-storage split-brain architecture, combining a high-performance relational/vector store (SQLite3) with a native property graph (Gorgonzola).

## 1. Storage & Indexing Engine

The storage engine splits state into two complementary embedded databases, optimizing for both fast text/vector search and deep topological traversal.

*   **SQLite3**: 
    *   Stores AST metadata, Git metrics, LOC counts, and OOD (Object-Oriented Design) metrics.
    *   **Search Engine (`c-fts-uring`)**: A custom C extension loaded natively into SQLite3. It provides true BM25F Full-Text Search and Dense Vector Similarity Search (HNSW). The extension operates using POSIX or Windows overlapping I/O fallback mechanisms for cross-platform compatibility.
*   **Gorgonzola Graph Database**:
    *   An openCypher graph layer built on top of Kùzu.
    *   Models the codebase topographically (e.g., `(Function)-[:CALLS]->(Function)`).
    *   Executes complex path-finding and structural queries.

> [!CAUTION]
> **Lack of Two-Phase Commit (2PC)**
> There is no distributed transaction coordinator between SQLite3 and Gorgonzola. The index pipeline writes sequentially to both. If the indexing process crashes or is interrupted mid-flight, the databases will drift out of sync, requiring a full index rebuild to repair orphaned nodes.

> [!WARNING]
> **Gorgonzola Thread Safety Constraints**
> The native Gorgonzola C++ `TaskScheduler` leaks worker threads if the database connection is repeatedly reopened in the same process. Heavy repeated usage across multi-session test suites may trigger `SIGSEGV` segmentation faults. Graph connections should be long-lived singletons or isolated to worker subprocesses.

## 2. Parsing & Language Intelligence

The indexing pipeline utilizes polyglot parsing backed by subprocess Language Server Protocol (LSP) clients.

*   **Tree-Sitter AST Parsing**: Fast syntax extraction across multiple languages (Python, Java, JS/TS, C++, Go, Rust, Swift, Ruby). Extracts symbols, classes, and methods.
*   **LSP Client Pool**: Spawns isolated, asynchronous subprocess language servers (`pyright`, `gopls`, `clangd`, etc.) on demand. These servers precisely resolve cross-file references and definitions.
*   **Heuristic Fallback**: If an LSP is unavailable, Pecorino falls back to name-and-suffix heuristic matching, which may generate noisy false-positive `CALLS` edges for common identifiers (e.g., `get`, `update`, `process`).

## 3. Retrieval & Ranking Pipeline

Pecorino's search endpoint implements a state-of-the-art hybrid pipeline.

1.  **Lexical + Semantic Retrieval**: Queries are scored using `c-fts-uring` BM25F and dense embeddings (`Xenova/all-MiniLM-L12-v2`).
2.  **Reciprocal Rank Fusion (RRF)**: Merges keyword and vector scores into a unified scale.
3.  **Graph Centrality**: Gorgonzola computes Structural PageRank and Leiden CPM community clusters dynamically. Nodes with higher global centrality receive boost weights.
4.  **Learning-To-Rank (LTR)**: A ranker extracts over 20 features for the top candidates, including temporal Git churn, instability (OOD), code complexity, and graph betweenness.
5.  **Cross-Encoder Reranking**: The very top results are reranked using an ONNX Cross-Encoder (**`ms-marco-MiniLM-L-12-v2`**) to provide deep semantic matching.
6.  **Heuristic Naming Analysis (LLM)**: Fallback logic uses an LLM (**`ollama/llama3`** via LiteLLM) if complex sampling fails.

> [!TIP]
> **Models Used Highlight**
> - **Embedding Model**: `Xenova/all-MiniLM-L12-v2` (384-dim local embeddings)
> - **Cross-Encoder Model**: `cross-encoder/ms-marco-MiniLM-L-12-v2`
> - **Fallback LLM**: `ollama/llama3` (for naming analyzer)

> [!NOTE]
> **Cold Start Latency**
> The retrieval pipeline imposes a 1-2 second cold-start latency when the ONNX inference models (Embedding + Cross-Encoder) are first loaded into memory.

## 4. Git & Temporal Analytics

Pecorino mines Git history via shell execution to understand the temporal volatility and coupling of the codebase.
*   **Metrics Extracted**: Co-change matrices (`FILE_CHANGES_WITH`), author entropy, and localized churn.
*   **Limitation**: Running temporal analytics across huge enterprise monorepos requires strict commit depth limits to prevent I/O and CPU saturation.

## Further Reading

For specific design decisions and the historical context of these architectural components, refer to the [Architecture Decision Records (ADRs)](adr/).
