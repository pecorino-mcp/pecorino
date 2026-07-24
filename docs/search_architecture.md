# Search & Ranking Architecture

Recent updates to the Pecorino architecture have significantly enhanced the search capabilities, transitioning from a basic full-text search to a robust, intent-driven, AI-powered ranking system. 

Configuration for embedding and reranking is handled via environment variables (e.g., `PECORINO_ENABLE_EMBEDDINGS`, `PECORINO_ENABLE_CROSS_ENCODER`, `PECORINO_CROSS_ENCODER_TOP_N`), keeping the architecture highly customizable.

## 1. Intent Router (`intent_router.py`)
To intelligently handle natural language queries, Pecorino now features a heuristic classifier that parses queries to determine the user's intent. The `IntentRouter` extracts the intended search mode using regex rules, ensuring queries like "who calls X?" are routed to the `callers` mode, and "dead code" routes to specific AST queries, rather than simple full-text matching. 
Supported heuristics include:
- Callers and Callees extraction
- Dependency / Impact tracing
- Semantic Neighborhood matching
- AST Intents (dead code, entry points, all classes)
- Cypher query pass-through

## 2. Bi-Encoder / Embedding Generation (`embedding.py` & `embedder.py`)
Pecorino uses local, ONNX-based Bi-Encoders (e.g., `nomic-embed-text-v1.5.onnx`) to convert code nodes into dense vector representations. These embeddings are stored efficiently in DuckDB's `code_nodes.embedding` array column, enabling extremely fast local vector search.

## 3. Hierarchical Code Graph Summarization — HCGS (`hcgs.py`)
To enrich symbols with architectural context without requiring external LLM API calls, Pecorino implements **Static HCGS** (`PECORINO_ENABLE_HCGS`):
- **Topological Level Ordering**: `build_levels()` queries call graph edges to organize functions/methods into dependency levels (Level 0 = leaf functions, Level 1 = caller functions).
- **Zero-LLM Context Propagation**: `process_levels_static()` propagates context upward from callees to callers, generating a hierarchical summary (`hcgs_summary`) for each code node.
- **Unified Downstream Integration**: The resulting `hcgs_summary` is stored in DuckDB, indexed in Tantivy (boost weight 3.0), re-embedded into vector space, and passed into Cross-Encoder scoring.

## 4. Hybrid Retrieval Pipeline (`index_db.py` & `tantivy_search.py`)
During a `hybrid` search, DuckDB and Tantivy power a multi-faceted retrieval engine:
- **Vector Retrieval**: Computes the query embedding and calculates cosine distances (`array_cosine_distance`) against node embeddings.
- **Full-Text Search (Tantivy BM25F)**: Runs true per-field BM25F matching (weighting `name`, `kind`, `summary`, etc. differently) using the Tantivy engine, falling back to DuckDB FTS if needed.
- **Reciprocal Rank Fusion (RRF)**: Merges the FTS and Vector ranks mathematically, scaled by PageRank or explicit boost IDs to surface universally strong candidates.
- **Empirical Performance (`scripts/benchmark_bm25f.py`)**: Tantivy BM25F delivers an average query latency of **8.85 ms** (with raw engine performance at **0.26 ms**), yielding a **1.16x speedup** over the DuckDB FTS baseline (**10.29 ms**).

## 5. Learning-to-Rank (LTR) Integration (`ltr_ranker.py`)
To properly weigh and blend the multitudes of metrics collected by Pecorino, the `ltr_ranker.py` introduces a Learning-to-Rank capability. It extracts up to 20 normalized features for any candidate result (including FTS/Vector Similarity, PageRank, Complexity, Git Churn, etc.). Candidates are preliminarily sorted by an LTR score generated via an XGBoost model or weighted linear fallback.

## 6. Cross-Encoder Reranking (`cross_encoder.py`)
If enabled, an ONNX-based Cross-Encoder (e.g., `ms-marco-MiniLM-L-12-v2`) provides deep semantic context understanding. To minimize latency, it only reranks the top `N` candidates. The model scores pair-wise matches by concatenating the candidate's name, summary, and body against the query. The top `N` candidates are re-sorted based on their logit-derived `ce_score`, providing high-accuracy final semantic matching.

## 7. Context Assembly & Enrichment (`context_assembler.py`)
To maximize LLM reasoning performance, retrieved symbols are enriched into full context bundles:
- **Structural Context**: Graph queries retrieve callers, callees, and parent scope (classes/modules).
- **Temporal Context**: Parses recent git commit history and automatically extracts linked issues/PRs (e.g., `#123`, `GH-456`, `PROJ-12`) directly from commit messages.
