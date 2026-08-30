# Agent Skills

Pecorino provides configurations, instructions, and workflows to help AI coding agents interact with the Pecorino MCP server.

---

## 1. The `setup_environment` Skill

The `setup_environment` skill (`.agents/skills/setup_environment/SKILL.md`) provides autonomous agents with an automated workflow to bootstrap, repair, and verify the local Pecorino development environment.

### Purpose & Capabilities
- **Resolves Virtualenv Inconsistencies**: Automatically fixes broken virtual environment interpreters caused by path migrations or virtualenv relocation.
- **Resolves Dependency Mismatches**: Reinstalls standard dependencies, editable packages, and git-based submodule SDKs (including `mcp-types`).
- **Compiles Native C/C++ Modules**: Builds the native `gorgonzola` (Kùzu C++ graph engine with OpenMP) and `c_fts_uring` (C-based FTS engine with `io_uring`) binaries if compiled artifacts are missing.
- **Automated Verification**: Runs the complete test suite and CLI validation to confirm environment readiness before proceeding with coding tasks.

### Invocation Workflow
Agents execute the provided script:
```bash
./scripts/setup_env.sh
```

This automates:
1. Cleaning and re-initializing the `.venv` directory.
2. Installing Python dependencies from `requirements.txt` and `mcp-types` from submodules.
3. Compiling native C++ `gorgonzola` (`make -C modules/gorgonzola python EXTENSION_LIST=""`) and `c_fts_uring`.
4. Copying compiled native libraries into `.venv/lib/python*/site-packages/`.
5. Installing `pecorino` in editable mode (`pip install -e .`) to provide `pecorino-mcp` and `pecorino` CLI binaries.

### Verification Checklist
```bash
# Execute test suite using the project virtualenv
.venv/bin/pytest tests/

# Verify the MCP server CLI
.venv/bin/pecorino-mcp --help
```

---

## 2. Agent Best Practices & Workflow Patterns

When interacting with a Pecorino MCP server, autonomous agents should follow structured patterns for maximum precision and minimal token usage:

### Pattern A: Workspace Ingestion & Re-indexing
- Always invoke `update_index` upon initial connection to a repository or after pulling large changesets.
- This populates the AST index, `c_fts_uring` inverted index, vector embeddings, and Gorgonzola graph database, ensuring all subsequent search and call-graph queries are up to date.

### Pattern B: Structural Exploration & Surgical Code Retrieval
- **Explore Structure**: Use `browse` with `view="tree"`, `"classes"`, or `"functions"` to understand file and module layouts.
- **Targeted Reading**: Use `browse` with `view="code"`, `start_line`, and `end_line` to retrieve exact code slices rather than dumping whole files.

### Pattern C: Multi-Mode Search & Call Graph Traversal
- **Natural Language**: Use `search` with `mode="auto"` to automatically route queries.
- **Callers & Callees**: Use `mode="callers"` to see who depends on a function before refactoring, and `mode="callees"` to inspect dependencies.
- **Symbol Usages**: Use `mode="usages"` to get both symbol definition and call sites in one invocation.
- **Multi-Hop Traversal**: Use `mode="trace"` to traverse deep dependency chains.
- **Preset Intents**: Use `mode="intent"` with `intent="dead_code"` or `intent="entry_points"` for repository audits.

### Pattern D: Graph Inquiries & NL-to-Cypher
- Use `query_graph` to run openCypher queries or ask natural language questions (e.g., `"Find all functions calling execute_query"`). The server automatically converts English into openCypher via IDE sampling or local fallback.

### Pattern E: Blast Radius & Impact Analysis
- Before finalizing code changes or creating pull requests, call `detect_changes` with `diff_target="HEAD"` to evaluate which AST symbols and downstream callers are impacted by the modifications.

### Pattern F: Architecture Decision Records (ADR)
- Maintain repository decisions by using `manage_adr` (`action="create"`, `"list"`, or `"read"`), keeping design rationale committed inside `docs/adr/`.
