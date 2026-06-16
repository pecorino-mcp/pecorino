import pytest
from src.gitstats_oopmetrics import parse, ClassDef, InterfaceDef, FunctionDef
from src.gitstats_tree_sitter_parser import parse_with_tree_sitter

def test_tree_sitter_python():
    # Verify we can call parse_with_tree_sitter directly for Python files
    code = """
import os
from sys import exit

class Animal:
    def __init__(self, name):
        self.name = name
        self.age = 0
        
    def speak(self):
        if self.name == "dog":
            print("woof")
        else:
            print("hello")
"""
    module = parse_with_tree_sitter(code, ".py")
    assert module is not None
    assert len(module.imports) == 2
    assert module.imports[0].module == "os"
    assert module.imports[1].module == "sys"
    assert module.imports[1].names == ["exit"]
    
    assert len(module.classes) == 1
    cls = module.classes[0]
    assert cls.name == "Animal"
    assert len(cls.methods) == 2
    
    # __init__ method
    init = [m for m in cls.methods if m.name == "__init__"][0]
    assert "name" in init.args
    assert "name" in init.accessed_attributes
    assert "age" in init.accessed_attributes
    
    # speak method (should have complexity = 2 due to the if statement)
    speak = [m for m in cls.methods if m.name == "speak"][0]
    assert speak.cyclomatic_complexity == 2

def test_tree_sitter_java():
    code = """
import java.util.List;
import java.io.*;

public class MyClass extends BaseClass implements InterfaceA {
    private int id;
    public String name;
    
    public void process(int val) {
        this.id = val;
        if (val > 10) {
            System.out.println("High");
        } else {
            System.out.println("Low");
        }
    }
}
"""
    # Force installation/compilation of Java grammar if not present, then parse
    module = parse_with_tree_sitter(code, ".java")
    if module is None:
        pytest.skip("Java tree-sitter grammar not available")
        
    assert len(module.imports) == 2
    assert module.imports[0].module == "java.util.List"
    
    assert len(module.classes) == 1
    cls = module.classes[0]
    assert cls.name == "MyClass"
    assert "BaseClass" in cls.bases
    assert "InterfaceA" in cls.bases
    
    # Attributes
    assert len(cls.attributes) == 2
    id_attr = [a for a in cls.attributes if a.name == "id"][0]
    assert id_attr.type_annotation == "int"
    assert id_attr.visibility == "private"
    
    # Method complexity and accessed attributes
    assert len(cls.methods) == 1
    proc = cls.methods[0]
    assert proc.name == "process"
    assert proc.cyclomatic_complexity == 2
    assert "id" in proc.accessed_attributes

def test_tree_sitter_rust_impl():
    code = """
struct Point {
    x: i32,
    y: i32,
}

impl Point {
    fn new(x: i32, y: i32) -> Point {
        Point { x, y }
    }
    
    fn distance(&self) -> f64 {
        if self.x > 0 {
            1.0
        } else {
            0.0
        }
    }
}
"""
    module = parse_with_tree_sitter(code, ".rs")
    if module is None:
        pytest.skip("Rust tree-sitter grammar not available")
        
    assert len(module.classes) == 1
    cls = module.classes[0]
    assert cls.name == "Point"
    
    assert len(cls.methods) == 2
    new_method = [m for m in cls.methods if m.name == "new"][0]
    dist_method = [m for m in cls.methods if m.name == "distance"][0]
    
    assert dist_method.cyclomatic_complexity == 2

def test_fallback_on_failure(monkeypatch):
    # Mock parse_with_tree_sitter to raise an exception,
    # ensuring parse() gracefully falls back to the legacy parser
    def mock_parse_ts(source, extension):
        raise RuntimeError("Tree-sitter error")
        
    monkeypatch.setattr("src.gitstats_tree_sitter_parser.parse_with_tree_sitter", mock_parse_ts)
    
    # Java legacy parser fallback should work
    code = "public class Hello {}"
    module = parse(code, ".java")
    assert module is not None
    assert len(module.classes) == 1
    assert module.classes[0].name == "Hello"

def test_class_bases_typescript():
    code = """
class MyClass extends BaseClass implements InterfaceA, InterfaceB<T> {
}
"""
    module = parse_with_tree_sitter(code, ".ts")
    if module is None:
        pytest.skip("TypeScript tree-sitter grammar not available")
    assert len(module.classes) == 1
    cls = module.classes[0]
    assert cls.name == "MyClass"
    assert cls.bases == ["BaseClass", "InterfaceA", "InterfaceB"]

def test_class_bases_python_native():
    code = """
from typing import Generic, TypeVar
T = TypeVar('T')

class MyPyClass(Generic[T], MyBase[int], abc.MyAbstract):
    pass
"""
    # Parse with python's native AST parser
    module = parse(code, ".py")
    assert len(module.classes) == 1
    cls = module.classes[0]
    assert cls.name == "MyPyClass"
    assert cls.bases == ["Generic", "MyBase", "abc.MyAbstract"]
