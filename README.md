# Pecorino MCP Server

A Model Context Protocol (MCP) server for deep Git history statistics, repository health tracking, and Object-Oriented Design (OOD) metrics analysis. 

Pecorino allows Large Language Models (LLMs) and dev tools (such as Claude Desktop or Cursor) to inspect codebases, analyze code structures, compute complexity/maintainability indexes, and detect risk hotspots.

---

## ✨ Features

- 🔌 **Model Context Protocol (MCP)**: Exposes 5 unified tools (`browse`, `search`, `update_index`, `set_workspace`, `metrics`) to your AI assistant.
- 📊 **Git History Analytics**: Commits, LOC growth, author contributions, activity patterns, and team performance tracking.
- 📐 **Object-Oriented Design Metrics**: Afferent/efferent coupling (Ca/Ce), instability (I), abstractness (A), and Distance-from-Main-Sequence (D) analysis.
- 🚨 **Risk Hotspot Detection**: Combines code churn (revision frequency) and complexity to pinpoint high-risk source files.
- 🗄️ **Fast DuckDB-backed AST Indexing**: Leverages tree-sitter to index class definitions, functions, and imports for rapid codebase navigation and search.
- 💻 **Flexible CLI & HTTP SSE**: Run as a standard CLI tool, start a local stdio MCP server, or deploy as a network-accessible SSE server.

---

## 🚀 Quick Start

### 1. Installation
Clone the repository recursively (to fetch the MCP SDK submodule) and set up the environment:

```bash
# Clone recursively
git clone --recursive https://github.com/pecorino-mcp/pecorino.git
cd pecorino

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies and package in editable mode
pip install -r requirements.txt
pip install -e .
```

### 2. Configure Claude Desktop
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

## 🛠️ Exposed MCP Tools

Once connected, your AI assistant can use the following tools:

### 1. `browse`
Browse codebase structure (tree, deps, classes, functions, pagerank, summary). Use this for structural viewing, not for searching or analysis.

### 2. `search`
Unified search and analysis tool with multiple modes:
- **`fts`** (default) — Full-text keyword search across the codebase.
- **`callers`** / **`callees`** — Call graph analysis. Who calls X? What does X call?
- **`impact`** — Deep dependency trace from a file or directory.
- **`usages`** — Combined search + callers in one call (find definition and all callers).
- **`intent`** — Preset AST queries: `all_classes`, `all_functions`, `entry_points`, `dead_code`, `files_by_language`.
- **`dsl`** — Custom JSON DSL query against the codebase AST and graph.
- **`functional-analysis`** — Functional purity analysis.

### 3. `update_index`
Update the AST index for the codebase and return a structural summary. Call this once after cloning or after significant changes.

### 4. `set_workspace`
Change the server's workspace root directory at runtime.

### 5. `metrics` *(Admin only)*
Calculate OOP metrics, cyclomatic complexity, or hotspot risk analysis. Use `what: ['hotspots']` for repo-level risk triage.

---

## 📂 Repository Layout

- `src/cli/` — Command-line interface and entry points.
- `src/core/` — Core metrics collector and configuration.
- `src/git/` — Git history and commit log parsers.
- `src/mcp_server/` — MCP server endpoints and core logic.
  - `src/mcp_server/index_pipeline.py` — Unified AST extraction and indexing pipeline.
  - `src/mcp_server/index_db.py` — DuckDB Full-Text Search (FTS) codebase index.
  - `src/mcp_server/gorgonzola_graph.py` — Gorgonzola graph database adapter.
  - `src/mcp_server/ramdisk.py` — `/dev/shm` RAM-disk staging for bulk indexing.
- `src/metrics/` — Maintainability index and Object-Oriented design metrics analyzers.
- `src/parsers/` — AST parsing (using Tree-sitter).
- `src/transports/` — MCP Adapters (stdio, fastAPI).
- `src/utils/` — Export formats and helper utilities.
- `modules/docs/` — Architecture and pipeline documentation.
- `tests/` — Automated test suites.

---

## 🖥️ Command Line Interface (CLI)

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

## 📄 License & Contributing

This project is licensed under the GNU Affero General Public License v3 (AGPL-3.0) - see the [LICENSE](LICENSE) file for details. Contributions, bug reports, and improvements are welcome! Open an issue or submit a pull request with small, focused changes.
