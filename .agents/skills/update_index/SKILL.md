---
name: update_index
description: Workflow for autonomous agents to incrementally update or rebuild the Pecorino codebase index and graph database.
---

# Updating the Pecorino Codebase Index

Use this workflow when source files have been added, modified, or removed, or when database indexes need synchronization with the current workspace state.

## Background Context

Pecorino maintains a dual-storage index in `.pecorino/`:
1. **SQLite3 (`index.db`)**: Stores AST metadata, symbol definitions, `c-fts-uring` full-text search indexes, and dense vector embeddings (`Xenova/all-MiniLM-L12-v2`).
2. **Gorgonzola (`graph/`)**: Embedded property graph storing structural AST relationships (`(Function)-[:CALLS]->(Function)`).

When files change, the index must be updated so semantic retrieval, hybrid RRF search, and graph traversal stay accurate.

## Execution Workflow

Agents can update the index either via the MCP tool or directly via the command line:

### Method 1: Via Pecorino MCP Tool (Recommended when running inside an MCP client)
Call the `update_index` tool:
```json
{
  "target": "/path/to/repo/or/subfolder",
  "allow_external": true
}
```

### Method 2: Direct Subprocess Execution (CLI / Terminal)
From the workspace root, run the index pipeline using the project virtualenv:

```bash
# Update entire repository
.venv/bin/python -m src.mcp_server.index_pipeline . .

# Update a specific subdirectory or file
.venv/bin/python -m src.mcp_server.index_pipeline . src/mcp_server/
```

> [!IMPORTANT]
> Always execute using `.venv/bin/python`. System Python will fail because `gorgonzola` and `mcp_types` are isolated in `.venv`.

## Verification

After updating the index:
1. Verify search retrieval works:
```bash
.venv/bin/pytest tests/test_hybrid_search.py
```
2. Check that the `.pecorino/` directory contains active `index.db` and `graph/` files.
