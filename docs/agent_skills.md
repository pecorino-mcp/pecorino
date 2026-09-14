# Autonomous Agent Skills & Deployment Workflows

Pecorino provides specialized configurations and instructions that autonomous agents (e.g., Antigravity, Claude Desktop, Cursor, Devin) can use to effectively interact with, update, and maintain the repository.

---

## 1. `setup_environment` Skill
Location: [`.agents/skills/setup_environment/SKILL.md`](file:///mnt/data/projects/pecorino/.agents/skills/setup_environment/SKILL.md)
- **Purpose**: Automates rebuilding and verifying the Pecorino local Python environment.
- **Capabilities**: Resolves `gorgonzola` C++ native library compilation and `mcp_types` package mismatches, ensuring agents have a functional, reproducible local environment before initiating complex coding or indexing tasks.
- **Command**: Run `./scripts/setup_env.sh`.

---

## 2. `update_index` Skill
Location: [`.agents/skills/update_index/SKILL.md`](file:///mnt/data/projects/pecorino/.agents/skills/update_index/SKILL.md)
- **Purpose**: Guides autonomous agents in updating the dual-storage AST and vector indexes after code modifications.
- **Workflow**:
  1. Detect modified files using `git status` or the MCP `detect_changes` tool.
  2. Invoke indexing using either the MCP `update_index` tool or direct CLI:
     ```bash
     .venv/bin/python -m src.mcp_server.index_pipeline . <target-path>
     ```
  3. Ensure models deployed for vector embeddings (`Xenova/all-MiniLM-L12-v2`) and cross-encoder reranking (`ms-marco-MiniLM-L-12-v2`) are loaded from `models/` or Hugging Face cache without downloading extraneous large models.

---

## 3. Architecture & ADR Maintenance
Location: [`docs/adr/`](file:///mnt/data/projects/pecorino/docs/adr/)
- **Purpose**: Ensures documentation never drifts from codebase reality when architectural decisions change.
- **Agent Workflow**:
  - When storage, parsing, ranking, or model choices shift, agents must update or create numbered ADRs (`000N-*.md`).
  - Use the MCP `manage_adr` tool (`list`, `create`, `view`) or edit records in [`docs/adr/`](file:///mnt/data/projects/pecorino/docs/adr/) directly.
  - Maintain honesty in "Brutal Realities & Flaws" sections—never hide known limitations, locking caveats, or platform dependencies.

---

## 4. Models Deployed by Agents
When interacting with search, ranking, or semantic classification pipelines, agents must expect and respect the following deployed models:
- **Embedding Model**: `Xenova/all-MiniLM-L12-v2` (384-dimensional dense vectors stored in SQLite3).
- **Cross-Encoder Model**: `cross-encoder/ms-marco-MiniLM-L-12-v2` (pairwise semantic reranking).
- **Fallback LLM**: `ollama/llama3` (invoked via LiteLLM for heuristic naming analysis).

---

## 5. Agent Verification Protocol
Before marking any maintenance task or code modification complete, agents must run the pytest suite:
```bash
.venv/bin/pytest tests/
```
Always use `.venv/bin/python` and `.venv/bin/pytest` to ensure native bindings (`gorgonzola`, `fts_uring`) load properly.
