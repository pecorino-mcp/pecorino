# Gitstats3 MCP Server

A Model Context Protocol (MCP) server for deep Git history statistics, repository health tracking, and Object-Oriented Design (OOD) metrics analysis. 

Gitstats3 allows Large Language Models (LLMs) and dev tools (such as Claude Desktop or Cursor) to inspect codebases, analyze code structures, compute complexity/maintainability indexes, and detect risk hotspots.

---

## ✨ Features

- 🔌 **Model Context Protocol (MCP)**: Exposes 4 unified tools (`browse`, `metrics`, `report`, `update_index`) to your AI assistant.
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
git clone --recursive https://github.com/lechibang-1512/gitstats3.git
cd gitstats3

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies and package in editable mode
pip install -r requirements.txt
pip install -e .
```

### 2. Configure Claude Desktop
Add Gitstats3 to your `claude_desktop_config.json`:

* **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
* **Linux:** `~/.config/Claude/claude_desktop_config.json`
* **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "gitstats3": {
      "command": "/path/to/gitstats3/.venv/bin/gitstats3-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "PYTHONPATH": "/path/to/gitstats3"
      }
    }
  }
}
```
*(Replace `/path/to/gitstats3` with your actual absolute path).*

---

## 🛠️ Exposed MCP Tools

Once connected, your AI assistant can use the following tools:

### 1. `/browse`
Inspects directories, files, or performs a semantic FTS search on the indexed codebase.
- **Parameters**:
  - `target` *(string, required)*: Absolute path to the file or directory.
  - `view` *(string, optional)*: `"summary"` (default), `"classes"`, `"functions"`, `"deps"` (imports), `"tree"` (directory tree), or `"search"` (semantic search).
  - `query` *(string, optional)*: The search term (required if `view` is `"search"`).

### 2. `/metrics`
Computes design metrics, cyclomatic complexity, Halstead metrics, or risk hotspots.
- **Parameters**:
  - `target` *(string, required)*: Absolute path to the file or folder.
  - `what` *(array of strings, optional)*: Metrics to run (`"oop"`, `"complexity"`, `"hotspots"`, or `"all"`).

### 3. `/report`
Runs a full repository scan and exports a structured JSON report directly to `<repo_name>_report/gitstats_metrics.json` inside your specified output directory.
- **Parameters**:
  - `repo_path` *(string, required)*: Absolute path to the Git repository.
  - `output_path` *(string, required)*: Absolute path to the output directory.

### 4. `/update_index`
Performs tree-sitter AST analysis and populates the DuckDB codebase index for fast semantic searching.
- **Parameters**:
  - `target` *(string, required)*: Absolute path to the file or folder to index.

---

## 📂 Repository Layout

- `src/cli/` — Command-line interface and entry points.
- `src/core/` — Core metrics collector and configuration.
- `src/git/` — Git history and commit log parsers.
- `src/mcp_server/` — MCP server endpoints and core logic.
  - `src/mcp_server/index.py` — DuckDB Full-Text Search (FTS) codebase index.
  - `src/mcp_server/gorgonzola_graph.py` — Gorgonzola graph database adapter.
- `src/metrics/` — Maintainability index and Object-Oriented design metrics analyzers.
- `src/parsers/` — AST parsing (using Tree-sitter).
- `src/transports/` — MCP Adapters (stdio, fastAPI).
- `src/utils/` — Export formats and helper utilities.
- `tests/` — Automated test suites.

---

## 🖥️ Command Line Interface (CLI)

You can also run Gitstats3 directly via the terminal:

```bash
# Start the stdio MCP server manually
gitstats3-mcp --transport stdio

# Start the SSE MCP server (requires Starlette & Uvicorn: pip install -e .[sse])
gitstats3-mcp --transport sse --host 127.0.0.1 --port 8000

# Run a CLI analysis and save the report inside a directory
python gitstats.py /path/to/repo /path/to/output_dir
```

For comprehensive CLI flags, transport details, and configuration options, see the [Local Server Deployment Guide](docs/local_server_deployment.md).

---

## 📄 License & Contributing

This project is licensed under the GPL-2.0 License. Contributions, bug reports, and improvements are welcome! Open an issue or submit a pull request with small, focused changes.
