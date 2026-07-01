---
name: setup_environment
description: Rebuild, verify, and fix the Pecorino local Python environment, resolving gorgonzola and mcp_types package mismatches.
---

# Rebuilding the Pecorino Dev Environment

Use this workflow when you encounter environment errors, virtualenv interpreter mismatches (e.g. `bad interpreter: No such file or directory` due to path changes), or missing database (`gorgonzola`) or MCP protocol modules (`mcp_types`).

## Background Context

This repository requires:
1. Standard Python libraries listed in `requirements.txt`.
2. `mcp-types` which is installed from the `modules/python-sdk` workspace subfolder (or from the git URL subdirectory `#subdirectory=src/mcp-types`).
3. `gorgonzola`—a custom-built native C++ database engine (Kùzu fork) located in `modules/gorgonzola/`. It must be compiled from source and copied to the `.venv`'s `site-packages` directory.
4. An editable installation of the `pecorino` project itself, generating the `pecorino-mcp` and `pecorino` CLI commands.

## Setup Workflow

To completely fix or rebuild the workspace environment, execute the provided setup script:

```bash
./bin/setup_env.sh
```

This script automates:
1. Removing the broken `.venv` and initializing a clean one.
2. Installing requirements (including `mcp-types` from git).
3. Running `make -C modules/gorgonzola python` to compile the C++ database library.
4. Safely copying the compiled `gorgonzola` Python module directory to the active `.venv` `site-packages`.
5. Running `pip install -e .` to register `pecorino-mcp` in the `.venv/bin/` folder.

## Verification

After the script completes, verify that both the tests pass and the MCP server starts:

```bash
# Run tests
.venv/bin/pytest tests/

# Verify the CLI tool resolves
.venv/bin/pecorino-mcp --help
```
