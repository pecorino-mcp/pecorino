# Agent Skills

Pecorino provides specialized configurations and instructions that autonomous agents can use to effectively interact with and maintain the project.

## 1. `setup_environment` Skill
A new `setup_environment` skill has been added to the repository (`.agents/skills/setup_environment/SKILL.md`).
- **Purpose**: Automates the rebuilding and verification of the Pecorino local Python environment.
- **Capabilities**: Resolves `gorgonzola` and `mcp_types` package mismatches, ensuring agents or developers have a functional, reproducible local environment before initiating complex coding tasks.
