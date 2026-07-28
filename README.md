# Pecorino MCP Server

A Model Context Protocol (MCP) server for deep Git history statistics, repository health tracking, and Object-Oriented Design (OOD) metrics analysis. 

Pecorino allows Large Language Models (LLMs) and dev tools (such as Claude Desktop or Cursor) to inspect codebases, analyze code structures, compute complexity/maintainability indexes, and detect risk hotspots.

---

##  Features

-  **Model Context Protocol (MCP)**: Exposes 5 unified tools (`browse`, `search`, `update_index`, `set_workspace`, `metrics`) to your AI assistant.
-  **Git History Analytics**: Commits, LOC growth, author contributions, activity patterns, and team performance tracking.
-  **Object-Oriented Design Metrics**: Afferent/efferent coupling (Ca/Ce), instability (I), abstractness (A), and Distance-from-Main-Sequence (D) analysis.
-  **Risk Hotspot Detection**: Combines code churn (revision frequency) and complexity to pinpoint high-risk source files.
-  **Fast DuckDB & Tantivy Indexing**: Leverages DuckDB for metadata and vector search, and Tantivy for true BM25F per-field full-text search.
-  **Flexible CLI & HTTP SSE**: Run as a standard CLI tool, start a local stdio MCP server, or deploy as a network-accessible SSE server.

---

##  Quick Start

### 1. System Prerequisites
Before setting up the environment, ensure you have the required build tools installed (needed to compile the native `gorgonzola` database module):

* **Fedora/RHEL**: `sudo dnf install cmake ninja-build gcc gcc-c++ ccache python3-devel`
* **Ubuntu/Debian**: `sudo apt install cmake ninja-build build-essential ccache python3-dev`
* **macOS**: `brew install cmake ninja ccache`

### 2. Installation
Clone the repository recursively (to fetch the MCP SDK submodule) and set up the environment:

```bash
# Clone recursively
git clone --recursive https://github.com/pecorino-mcp/pecorino.git
cd pecorino

# Run the environment setup script
# This will initialize the .venv, install dependencies, and compile the gorgonzola module
./scripts/setup_env.sh

# Activate the virtual environment
source .venv/bin/activate
```

### 3. Configure Claude Desktop
Add Pecorino to your `claude_desktop_config.json`:

* **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
* **Linux:** `~/.config/Claude/claude_desktop_config.json`
* **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pecorino": {
      "command": "/path/to/pecorino/.venv/bin/pecorino-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "PYTHONPATH": "/path/to/pecorino"
      }
    }
  }
}
```
*(Replace `/path/to/pecorino` with your actual absolute path).*

---

##  Exposed MCP Tools

Once connected, your AI assistant can use the following tools:

### 1. `browse`
Browse codebase structure (tree, deps, classes, functions, pagerank, summary) or retrieve specific lines of code with `view="code"`. Use this for structural viewing and code retrieval.

### 2. `search`
Unified search and analysis tool with multiple modes:
- **`hybrid`** (default) — Blended vector similarity + Tantivy BM25F FTS with RRF and LTR scoring.
- **`fts`** — Full-text keyword search across the codebase.
- **`callers`** / **`callees`** — Call graph analysis. Who calls X? What does X call?
- **`impact`** — Deep dependency trace from a file or directory.
- **`usages`** — Combined search + callers in one call (find definition and all callers).
- **`intent`** — Preset AST queries: `all_classes`, `all_functions`, `entry_points`, `dead_code`, `files_by_language`.
- **`community`** — Semantic neighborhood and cluster discovery via Leiden community detection.
- **`explain`** — Ranking explanation surfacing why a symbol scored highly.
- **`snippet`** / **`trace`** — Contextual code snippet retrieval and deep path tracing.
- **`cypher`** — Embedded Cypher query execution within search.
- **`auto`** — Automatic natural language query intent routing.
- **`dsl`** — Custom JSON DSL query against the codebase AST and graph.
- **`functional-analysis`** — Functional purity analysis.

### 3. `query_graph`
Execute openCypher queries directly against the Kùzu/Gorgonzola knowledge graph for deep structural and similarity analysis.

### 4. `update_index`
Update the AST index for the codebase and return a structural summary. Call this once after cloning or after significant changes.

### 5. `set_workspace`
Change the server's workspace root directory at runtime.

### 6. `metrics` *(Admin only)*
Calculate OOP metrics, cyclomatic complexity, or hotspot risk analysis. Use `what: ['hotspots']` for repo-level risk triage.

### 7. `detect_changes`
Detect modified files and git reflog changes to track incremental updates.

### 8. `manage_adr`
Create, list, and query Architecture Decision Records (ADRs) within the codebase.

### 9. `manage_snapshot`
Take structural snapshots of the codebase graph and index for historical comparison and diffing.

---

##  Architecture

Pecorino relies on a multi-stage **Dual Index & Graph Architecture** to extract, index, and analyze codebase structure:

1. **AST Parsing (`src/parsers/`)**: Uses `py-tree-sitter` to parse source files across multiple languages, extracting definitions for modules, classes, interfaces, functions, and imports.
2. **Graph Knowledge Base (`src/mcp_server/gorgonzola_graph.py`)**: Stores code structures, call graphs, dependencies, and caller/callee relationships as an openCypher property graph powered by the `gorgonzola` / Kùzu engine.
3. **Dual Search Engine (Relational, Vector & Tantivy BM25F) (`src/mcp_server/index_db.py` & `tantivy_search.py`)**: 
   - **Bi-Encoder Embeddings (`embedding.py`)**: Dense vector representations (`nomic-embed-text-v1.5`) stored in DuckDB array columns for fast cosine similarity.
   - **Tantivy BM25F (`tantivy_search.py`)**: Native per-field BM25F full-text search with independent field boosting (`name`, `kind`, `summary`, `filepath`, `body`).
4. **AI Reranking & Learning-to-Rank (`src/mcp_server/cross_encoder.py` & `ltr_ranker.py`)**: 
   - **Cross-Encoder Reranking**: Pairwise ONNX cross-encoder (`ms-marco-MiniLM-L-12-v2`) reranking the top candidate results.
   - **LTR Model**: Blends 20+ signals (PageRank, Betweenness Centrality, OOD metrics, Git churn/authorship entropy).
5. **Git Analytics Engine (`src/git/`, `src/core/`)**: Parses reflogs and commit histories to build temporal coupling graphs and track code churn.
6. **MCP Transport Layer (`src/transports/`)**: Exposes index, vector, and graph databases over standard Model Context Protocol (stdio, SSE) to AI agents.

##  Core Functions (Python API)

Beyond the CLI and MCP Server, Pecorino can be used directly as a Python library. Key modules and functions exported via `src/__init__.py` include:

- **Metrics & Complexity**: 
  - `calculate_maintainability_index()`, `calculate_mccabe_complexity()`, `calculate_halstead_metrics()`
  - `OOPMetricsAnalyzer` (computes Ca, Ce, I, A, D)
- **Git Analytics**: 
  - `GitDataCollector` (harvests commit history and LOC metrics)
  - `HotspotDetector`, `analyze_hotspots()` (identifies churn vs. complexity risks)
- **AST Manipulation**:
  - `TreeSitterGrammarManager` (manages language grammars)
  - `ASTNode`, `ClassDef`, `FunctionDef`, `walk()`, `iter_child_nodes()`
- **Export & Utilities**:
  - `MetricsExporter`, `export_to_json()`, `export_to_yaml()`

##  Repository Layout

- `src/cli/` — Command-line interface and entry points.
- `src/core/` — Core data collectors and configuration.
- `src/git/` — Git history and commit log parsers.
- `src/mcp_server/` — MCP server endpoints and core logic.
  - `src/mcp_server/index_pipeline.py` — Unified AST extraction and indexing pipeline.
  - `src/mcp_server/index_db.py` — DuckDB Full-Text Search (FTS) codebase index.
  - `src/mcp_server/gorgonzola_graph.py` — Gorgonzola graph database adapter.
  - `src/mcp_server/ramdisk.py` — `/dev/shm` RAM-disk staging for bulk indexing.
- `src/metrics/` — Maintainability, complexity, and OOP metrics analyzers.
- `src/parsers/` — AST parsing (using Tree-sitter).
- `src/transports/` — MCP Adapters (stdio, fastAPI).
- `src/utils/` — Export formats and helper utilities.
- `modules/` — Git submodules including `gorgonzola`, `py-tree-sitter`, and `pecorino-utils`.
- `docs/` — Architectural documentation (Search, Indexing, Observability & Security, Agent Skills, Evolution Timeline).
- `tests/` — Automated test suites.

---

##  Command Line Interface (CLI)

You can also run Pecorino directly via the terminal:

```bash
# Start the stdio MCP server manually
pecorino-mcp --transport stdio

# Start the SSE MCP server (requires Starlette & Uvicorn: pip install -e .[sse])
pecorino-mcp --transport sse --host 127.0.0.1 --port 8000

# Run a CLI analysis and save the report inside a directory
python pecorino.py /path/to/repo /path/to/output_dir
```

For comprehensive CLI flags, transport details, and configuration options, see the [Local Server Deployment Guide](https://github.com/pecorino-mcp/pecorino-docs/blob/main/local_server_deployment.md).

---

##  License & Contributing

This project is licensed under the GNU Affero General Public License v3 (AGPL-3.0) - see the [LICENSE](LICENSE) file for details. Contributions, bug reports, and improvements are welcome! Open an issue or submit a pull request with small, focused changes.
