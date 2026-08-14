# 6. Graph Centrality, Leiden Community Detection, and Static HCGS

Date: 2026-08-14

## Status

Accepted

## Context

A large codebase contains thousands of symbols, but only a small fraction represent critical architectural hubs, entry points, or core domain abstractions. LLMs and developers need structural ranking and high-level architectural summaries without burning LLM tokens during batch indexing.

## Decision

Implement structural graph analytics directly on Gorgonzola / Kùzu:
1. **PageRank**: Calculate structural centrality over `CALLS`, `INHERITS`, and `DEPENDS_ON` edges to identify high-centrality foundational components.
2. **Leiden Community Detection**: Project the code graph (`CodeGraph`) and run resolution sweep algorithms (`sweep_gamma`, `find_stable_partition`) to identify cohesive architectural modules and subsystem clusters.
3. **Static Hierarchical Code Graph Summarization (HCGS)**: Perform topological level ordering (`build_levels`) and propagate callee summary signals upwards to callers without LLM calls, storing hierarchical context in DuckDB and Tantivy.

## Consequences & Trade-offs

### Positive
- Objective, mathematical identification of core architecture nodes (PageRank) and subsystem boundaries (Leiden communities).
- Fast, zero-cost (no LLM API billing) hierarchical summarization during index time.

### Brutal Realities & Flaws
- **Graph Projection Overhead**: Running projected graph sweeps in Kùzu requires allocating temporary in-memory graph projections (`PROJECT_GRAPH` / `DROP_PROJECTED_GRAPH`). Failed projections or abnormal crashes leave locks that must be explicitly cleaned up.
- **Topological Cycle Degeneracy**: Circular dependencies (mutual recursion or circular imports) violate DAG assumptions in topological level builders, requiring cycle-breaking heuristics.
- **Static Summary Quality**: Rule-based callee-to-caller context aggregation is syntactically informative but lacks the semantic nuance of an actual LLM-generated summary.
