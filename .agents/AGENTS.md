# Pecorino Project Rules

- Always use `.venv/bin/python` and `.venv/bin/pytest` (not the system Python) when running Python code or tests in this project. The project depends on custom native packages (`gorgonzola`) and an editable install that are only available in the virtualenv.
- When updating the codebase or modifying indexed structures, agents must update the AST index using `.venv/bin/python -m src.mcp_server.index_pipeline` or the `update_index` MCP tool. Refer to `.agents/skills/update_index/SKILL.md`.
- Active models:
  - Embedding: `Xenova/all-MiniLM-L12-v2`
  - Cross-Encoder: `cross-encoder/ms-marco-MiniLM-L-12-v2`
  - Fallback LLM: `ollama/llama3`
- Never allow documentation to drift: update relevant ADRs in `docs/adr/` whenever architecture or storage designs change.
