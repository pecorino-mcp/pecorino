# Implementation Plan: Tree-Sitter Parsing Optimization

This document outlines the proposed implementation plan for the final identified bottleneck in the Pecorino indexing pipeline: CPU overhead caused by Python-side AST traversal and string allocations.

## Current State & Bottleneck

Currently, `pecorino` uses the `tree-sitter` Python bindings to parse source files into a C-backed syntax tree. However, it extracts metrics and structures using a **full recursive walk** written in Python (`TreeSitterExtractor.traverse()` in `tree_sitter_parser.py`). 

This causes two major issues during ingestion:
1. **Python Recursion Overhead:** Walking thousands of AST nodes per file in Python is significantly slower than letting the underlying C/Rust engine perform the traversal.
2. **String Allocations:** Extracting node text currently uses `self.source[node.start_byte:node.end_byte].decode('utf-8')`. This forces Python to allocate a new string object for nearly every node inspected.

## Proposed Optimization

Instead of a Python-side recursive walk, we will transition to using **Tree-Sitter Language Queries** (`.scm` query files). Tree-sitter queries allow us to specify patterns (e.g., `(function_definition name: (identifier) @name body: (block) @body)`) that the C-core executes natively, returning exactly the nodes we care about in a single batch.

### Implementation Steps

#### Phase 1: Query Definition
1. **Source Community Queries:** We will not write basic queries from scratch. We will clone or download the standard `tags.scm` files provided by the official tree-sitter language repositories for all 8 supported languages (Python, Java, JS/TS, C/C++, Go, Rust, Swift).
2. **Extend Queries for OOP Metrics:** The community `tags.scm` files identify class and function definitions, but we must augment them to capture:
   - Base classes / interfaces (`(class_definition superclasses: (...) @base)`)
   - Method parameters and return types
   - Method invocations (`(call_expression function: (attribute) @call)`) for call graph building.
   - Control flow nodes (`if`, `while`, `for`) for cyclomatic complexity calculation.

#### Phase 2: Refactoring `TreeSitterExtractor`
1. Replace `self.traverse(tree.root_node)` with batch query executions:
   ```python
   query = lang.query(LANGUAGE_QUERIES[self.language])
   captures = query.captures(tree.root_node)
   ```
2. **Process Captures:** Iterate through the flat list of returned captures to assemble `ClassDef`, `FunctionDef`, and `ImportDef` objects.
3. **Defer String Decoding:** Stop decoding bytes to strings indiscriminately. Only decode node text (`node.text.decode('utf8')`) when the node is specifically captured as an identifier, name, or parameter.

#### Phase 3: Rollout & Testing
Since changing the parser is high-risk, we will adopt a hybrid rollout:
1. Implement the Tree-Sitter Query engine for **Python only** as a proof of concept.
2. Run identical repositories through the legacy recursive parser and the new query parser. Compare output dictionaries to assert exact structural equivalence (zero regressions).
3. If successful, systematically expand the query files to support the remaining 7 languages. 

## Expected Impact
By moving the AST traversal into the C-core and eliminating thousands of redundant Python string allocations per file, the AST parsing phase should become nearly instantaneous, shifting the bottleneck entirely to the file reading (I/O) phase.
