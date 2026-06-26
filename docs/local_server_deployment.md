# Pecorino Local Server Deployment & Single-Machine Guide

This guide details how to deploy and run the **Pecorino Model Context Protocol (MCP) server** locally on a single machine. The MCP server enables Large Language Models (LLMs) and dev assistant clients to browse codebases, compute metrics, perform semantic searches, and generate repository reports.

---

## ⚙️ Prerequisites & System Requirements

Before setting up the Pecorino server, ensure your machine meets the following:

- **Operating System:** Linux, macOS, or Windows (via WSL or native Python).
- **Python:** Version `3.8` or newer (Python `3.10+` recommended for optimized performance).
- **Git:** Git CLI must be installed and accessible via your system's `PATH`.
- **LLM Client:** An MCP-compatible client such as Claude Desktop, Cursor, VS Code (via MCP plugins), or the MCP Inspector.

---

## 🚀 Installation & Environment Setup

Follow these steps to set up Pecorino and its MCP dependencies on your local machine:

### 1. Clone the Repository (with Submodules)
Pecorino relies on the Model Context Protocol Python SDK, which is tracked as a Git submodule. Clone the repository recursively:

```bash
git clone --recursive https://github.com/pecorino/pecorino.git
cd pecorino
```

> [!TIP]
> If you have already cloned the repository without submodules, run the following command in the root folder to initialize and fetch them:
> ```bash
> git submodule update --init --recursive
> ```

### 2. Create and Activate a Virtual Environment
We recommend installing all packages inside a Python virtual environment to avoid version conflicts:

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
# On Linux / macOS:
source .venv/bin/activate
# On Windows (Command Prompt):
.venv\Scripts\activate.bat
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### 3. Install Package Dependencies
Install the required packages, including parser modules and tree-sitter grammars:

```bash
pip install -r requirements.txt
```

### 4. Install Pecorino
Install the package in editable mode to register command-line entry points:

```bash
pip install -e .
```

---

## 🏃 Running the MCP Server

Pecorino supports two transport protocols for single-machine deployments: **Standard I/O (`stdio`)** and **Server-Sent Events (`sse`)**.

### Option A: Standard I/O (`stdio`) Transport (Recommended)
In `stdio` mode, the MCP client (such as Claude Desktop) starts the server as a child process and communicates via standard input/output. This is the simplest setup and does not require opening network ports.

Run using the registered entry point:
```bash
pecorino-mcp --transport stdio
```

Or run via the main entry script:
```bash
python pecorino.py --mcp --transport stdio
```

---

### Option B: Server-Sent Events (`sse`) Transport
In `sse` mode, Pecorino runs as a persistent HTTP server. Clients connect to the server over the network. This is useful for sharing a single local instance across multiple development tools.

> [!IMPORTANT]
> The `sse` transport requires `starlette` and `uvicorn` web server packages. You can install them by running:
> ```bash
> pip install -e .[sse]
> ```

Start the server:
```bash
pecorino-mcp --transport sse --host 127.0.0.1 --port 8000
```

This starts a server listening on `http://127.0.0.1:8000` with the following endpoints:
- `GET http://127.0.0.1:8000/sse` — SSE connection endpoint.
- `POST http://127.0.0.1:8000/messages/` — POST channel for client messages.

---

## 🖥️ Integrating with LLM Clients

### Claude Desktop Integration
To use Pecorino with Claude Desktop, add the server configuration to your `claude_desktop_config.json` file.

* **File Location:**
  * **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
  * **Linux:** `~/.config/Claude/claude_desktop_config.json`
  * **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

#### 📝 Stdio Server Configuration
```json
{
  "mcpServers": {
    "pecorino": {
      "command": "/path/to/pecorino/.venv/bin/pecorino-mcp",
      "args": [
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "/path/to/pecorino"
      }
    }
  }
}
```

Replace `/path/to/pecorino` with the actual absolute path to your cloned repository.

---

## 🛠️ Exposed MCP Tools

Once the server is connected, the LLM client will have access to the following 4 tools:

### 1. `browse`
Inspects directories, files, imports, or performs a semantic FTS search.
- **Parameters:**
  - `target` *(string, required)*: Absolute path to the file or directory.
  - `view` *(string, optional)*:
    - `"summary"` *(default)*: Lists high-level stats, classes, and functions.
    - `"classes"`: Detailed overview of classes and their attributes/methods.
    - `"functions"`: Lists all functions, parameters, and sizes.
    - `"deps"`: File dependency graph based on import statements.
    - `"tree"`: Recursive directory tree.
    - `"search"`: Performs FTS full-text search on the indexed codebase.
  - `query` *(string, optional)*: Required if `view` is `"search"`.
  - `limit` *(integer, optional)*: Max number of search results (default: `10`).

### 2. `metrics`
Computes design, complexity, or hotspot metrics on a target file or folder.
- **Parameters:**
  - `target` *(string, required)*: Absolute path to the target codebase folder or file.
  - `what` *(array of strings, optional)*: Metrics to calculate. Can contain `"oop"`, `"complexity"`, `"hotspots"`, or `"all"` (default: `["all"]`).

### 3. `report`
Runs a full analysis scan over a git repository and exports the metrics dataset directly to a repository-specific subdirectory (`<repo_name>_report/pecorino_metrics.json`) inside the specified output directory.
- **Parameters:**
  - `repo_path` *(string, required)*: Absolute path to the git repository.
  - `output_path` *(string, required)*: Absolute path to the output directory.

### 4. `update_index`
Indexes or re-indexes the AST of a target file or folder, storing the metadata in a local DuckDB database for fast semantic searches.
- **Parameters:**
  - `target` *(string, required)*: Absolute path to the file or folder.

---

## 🗄️ Database Storage & Indexing

Under the hood, Pecorino uses a **DuckDB FTS (Full-Text Search)** database to index AST nodes.
- **Location:** Index databases are stored per-repository under your home directory:
  `~/.pecorino/indexes/<repo_md5_hash>_code_search.duckdb`
- **Reindexing:** To ensure search queries return accurate results, run the `update_index` tool whenever codebase changes are made.
