import pytest
from pathlib import Path
import tree_sitter
from src.tsgm import TreeSitterGrammarManager

def test_tsgm_lifecycle():
    # Initialize manager
    manager = TreeSitterGrammarManager()
    
    # Load language
    lang = manager.get_language("python")
    assert lang is not None
    
    # Parse code snippet
    parser = tree_sitter.Parser(lang)
    
    code = b"def hello_world():\n    print('Hello, World!')\n"
    tree = parser.parse(code)
    
    assert tree.root_node is not None
    assert tree.root_node.type == "module"
    
    # Verify we can find function definition in AST
    func_node = tree.root_node.children[0]
    assert func_node.type == "function_definition"
    name_node = func_node.child_by_field_name("name")
    assert name_node is not None
    assert code[name_node.start_byte:name_node.end_byte] == b"hello_world"
