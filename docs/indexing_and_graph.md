# Indexing Pipeline & Graph Architecture

Recent architectural improvements have focused on performance observability, scaling, incremental updates, and graph schema simplification.

## 1. Indexing Profiler & Configurable Parameters
The indexing pipeline now supports deep observability via the `IndexProfiler`, which tracks performance across extraction and chunking phases.
- Parameters such as the number of parallel workers and chunking configurations are now highly customizable.
- Object-Oriented Design (OOD) feature tracking (including **Betweenness Centrality** for identifying architectural bottlenecks) has been deeply integrated into the pipeline.
- Bulk edge insertion is now automatically deduplicated to preserve graph integrity.
- Index size estimation utilities have been added to provide better insights during large repository indexing.

## 2. Incremental Indexing & Dynamic Scanning
To support rapidly changing codebases without full re-indexing overhead:
- **File Watchers**: An incremental indexing file watcher detects live file modifications and updates only the changed AST nodes and embeddings in real-time.
- **Dynamic Directory Scanning**: Repositories are now dynamically scanned to ensure newly added or removed directories are synced instantly with the index database.

## 3. Graph Schema Simplification
Graph database queries in Neo4j/Gorgonzola have been optimized.
- We simplified the schema by migrating back to a dedicated `File` label instead of relying on a generic `CodeNode` with a `kind` property. This reduces cypher query complexity and accelerates structural graph traversals.

## 4. Git Metric Synchronization
Git history metrics (such as temporal coupling, ownership entropy, and stable churn metrics) are now robustly extracted and synchronized directly into the index databases. This allows the search and LTR ranking pipelines to seamlessly query historical volatility.
