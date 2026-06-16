#!/usr/bin/env python3
"""
run_tree_sitter.py

Run Tree-sitter parsing on files in this repository.
It installs the Python tree-sitter grammar, compiles it, and parses the source code
to extract and display classes and functions.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tsgm import TreeSitterGrammarManager
from tree_sitter import Parser

def print_limited_tree(node, source_code: bytes, max_nodes: int, current_count: list = None, indent: int = 0):
    if current_count is None:
        current_count = [0]
    if current_count[0] >= max_nodes:
        if current_count[0] == max_nodes:
            print("  " * indent + "...")
            current_count[0] += 1
        return
        
    current_count[0] += 1
    node_type = node.type
    start = node.start_byte
    end = node.end_byte
    text = source_code[start:end].decode('utf-8', errors='ignore').strip().replace('\n', ' ')
    if len(text) > 45:
        text = text[:42] + "..."
    
    print("  " * indent + f"{node_type}: '{text}'")
    for child in node.children:
        print_limited_tree(child, source_code, max_nodes, current_count, indent + 1)

def count_nodes(node) -> int:
    return 1 + sum(count_nodes(child) for child in node.children)

def main():
    print("=== Tree-sitter Grammar Manager Demo ===")
    
    # Initialize the manager. It will use the 'grammars' folder in workspace root.
    manager = TreeSitterGrammarManager()
    print(f"Grammar cache directory: {manager.cache_dir}")
    print(f"Library path: {manager.lib_path}")
    
    # 1. Install python grammar (if not already cached)
    print("\n1. Installing Python grammar...")
    try:
        manager.install("python")
        print("Python grammar installed successfully.")
    except Exception as e:
        print(f"Error installing Python grammar: {e}")
        sys.exit(1)
        
    # 2. Build the grammar library
    print("\n2. Compiling grammar library...")
    try:
        manager.build_all()
        print("Grammar library compiled successfully.")
    except Exception as e:
        print(f"Error compiling grammar library: {e}")
        sys.exit(1)
        
    # 3. Load the language
    print("\n3. Loading Python language module...")
    try:
        py_lang = manager.get_language("python")
        print("Python language module loaded.")
    except Exception as e:
        print(f"Error loading Python language: {e}")
        sys.exit(1)
        
    # 4. Parse a file from the repository
    # Let's parse src/tsgm.py itself
    target_file = project_root / "src" / "tsgm.py"
    print(f"\n4. Parsing file: {target_file.relative_to(project_root)}")
    
    try:
        with open(target_file, "rb") as f:
            source_content = f.read()
            
        parser = Parser()
        parser.set_language(py_lang)
        tree = parser.parse(source_content)
        
        # Traverse AST and collect some stats
        classes = []
        functions = []
        
        def traverse(node):
            if node.type == 'class_definition':
                name_node = node.child_by_field_name('name')
                if name_node:
                    classes.append(source_content[name_node.start_byte:name_node.end_byte].decode('utf-8'))
            elif node.type == 'function_definition':
                name_node = node.child_by_field_name('name')
                if name_node:
                    functions.append(source_content[name_node.start_byte:name_node.end_byte].decode('utf-8'))
            for child in node.children:
                traverse(child)
                
        traverse(tree.root_node)
        
        print("\n=== Parsing Summary ===")
        print(f"Total AST Nodes: {count_nodes(tree.root_node)}")
        print(f"Found {len(classes)} classes: {', '.join(classes)}")
        print(f"Found {len(functions)} functions: {', '.join(functions)}")
        
        print("\n=== AST Structure (First few nodes) ===")
        print_limited_tree(tree.root_node, source_content, max_nodes=25)
        
    except Exception as e:
        print(f"Error parsing file: {e}")

if __name__ == "__main__":
    main()
