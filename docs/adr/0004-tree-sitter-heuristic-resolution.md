# 4. Tree-Sitter AST Extraction & Heuristic Symbol Resolution

Date: 2026-08-14

## Status

Accepted

## Context

Static code analysis in polyglot environments must tolerate syntax errors, incomplete files, dynamic typing, and missing build environments. Full compiler toolchains (clang, rustc, tsc) are too slow and fail if external dependencies are missing.

## Decision

Utilize Tree-Sitter as the core AST extraction engine:
1. Parse source code into concrete syntax trees using language-specific Tree-Sitter grammars (Python, TypeScript, JavaScript, Rust, Go, C/C++, Java).
2. Extract definitions (`Function`, `Method`, `Class`, `Variable`, `Lambda`, `ControlFlow`), docstrings, cyclomatic complexity, and call sites (`Symbol`).
3. Resolve intra-repo dependencies through language-aware heuristic path matching (`_resolve_dependency`, `_find_file_in_repo`).
4. Resolve `Symbol` call nodes to concrete `Function`/`Method` nodes via post-indexing openCypher unification queries (`_post_process_graph`).

## Consequences & Trade-offs

### Positive
- Extremely fast, error-tolerant AST parsing across all major programming languages.
- Runs purely on raw source text without requiring project compilation, dependency installations, or build tools.

### Brutal Realities & Flaws
- **Heuristic Inaccuracies**: In dynamic languages (Python, JavaScript), method calls on polymorphic or dynamically typed objects (e.g. `obj.run()`) cannot be disambiguated with pure AST parsing. They are linked to *all* methods named `run()` across the codebase during post-processing.
- **Dotted Name Resolution Ambiguity**: Dotted member access resolution (`a.b.c()`) relies on suffix-matching queries (`WHERE s.name = m.name OR ends_with(s.name, '.' + m.name)`), which creates false-positive `CALLS` edges in large codebases with common method names (`get`, `init`, `process`).
- **No Type-Aware Macro/Template Expansion**: C++ template instantiations, Rust declarative macros, and Python decorator metaprogramming remain partially opaque to pure Tree-Sitter grammars.
