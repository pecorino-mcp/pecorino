"""
AST Node classes for the multi-language parser.

Provides dataclass-based AST nodes inspired by Python's ast module,
supporting Python, Java, JavaScript/TypeScript, C++, Go, Rust, and Swift.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Iterator, Set


@dataclass
class ASTNode:
    """Base class for all AST nodes, inspired by Python's ast.AST."""
    lineno: int = 0
    col_offset: int = 0
    end_lineno: int = 0
    end_col_offset: int = 0
    
    @property
    def _fields(self) -> tuple:
        """Returns names of child node fields (like Python's ast)."""
        return ()


@dataclass
class ImportDef(ASTNode):
    """Represents an import statement."""
    module: str = ""
    names: List[str] = field(default_factory=list)
    is_from: bool = False  # True for 'from X import Y'
    
    @property
    def _fields(self) -> tuple:
        return ('module', 'names')


@dataclass 
class AttributeDef(ASTNode):
    """Represents a class attribute/field."""
    name: str = ""
    type_annotation: Optional[str] = None
    visibility: str = "public"  # public, private, protected
    
    @property
    def _fields(self) -> tuple:
        return ('name', 'type_annotation', 'visibility')


@dataclass
class FunctionDef(ASTNode):
    """Represents a function or method definition."""
    name: str = ""
    args: List[str] = field(default_factory=list)
    return_type: Optional[str] = None
    decorators: List[str] = field(default_factory=list)
    is_abstract: bool = False
    is_static: bool = False
    visibility: str = "public"
    body_start: int = 0
    body_end: int = 0
    cyclomatic_complexity: int = 1
    called_methods: Set[str] = field(default_factory=set)
    accessed_attributes: Set[str] = field(default_factory=set)
    parameter_types: List[str] = field(default_factory=list)
    
    @property
    def _fields(self) -> tuple:
        return ('name', 'args', 'decorators')


@dataclass
class ClassDef(ASTNode):
    """Represents a class definition."""
    name: str = ""
    bases: List[str] = field(default_factory=list)
    methods: List[FunctionDef] = field(default_factory=list)
    attributes: List[AttributeDef] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    is_abstract: bool = False
    is_interface: bool = False  # For Java interfaces, TS interfaces, Go interfaces
    nested_classes: List['ClassDef'] = field(default_factory=list)
    wmc: int = 0
    dit: int = 0
    noc: int = 0
    cbo: int = 0
    rfc: int = 0
    lcom: int = 0
    coupled_classes: Set[str] = field(default_factory=set)
    
    @property
    def _fields(self) -> tuple:
        return ('name', 'bases', 'methods', 'attributes', 'decorators', 'nested_classes')


@dataclass
class InterfaceDef(ASTNode):
    """Represents an interface (Java, TypeScript, Go, Swift protocol, Rust trait)."""
    name: str = ""
    methods: List[FunctionDef] = field(default_factory=list)
    extends: List[str] = field(default_factory=list)
    
    @property
    def _fields(self) -> tuple:
        return ('name', 'methods', 'extends')


@dataclass
class ModuleDef(ASTNode):
    """Root node representing a source file/module."""
    name: str = ""
    imports: List[ImportDef] = field(default_factory=list)
    classes: List[ClassDef] = field(default_factory=list)
    interfaces: List[InterfaceDef] = field(default_factory=list)
    functions: List[FunctionDef] = field(default_factory=list)
    
    @property
    def _fields(self) -> tuple:
        return ('imports', 'classes', 'interfaces', 'functions')


# =============================================================================
# AST Utilities
# =============================================================================

def walk(node: ASTNode) -> Iterator[ASTNode]:
    """
    Recursively yield all nodes in the tree starting at node.
    Similar to Python's ast.walk().
    
    Args:
        node: The root AST node to walk
        
    Yields:
        All nodes in the tree
    """
    yield node
    for field_name in node._fields:
        value = getattr(node, field_name, None)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, ASTNode):
                    yield from walk(item)
        elif isinstance(value, ASTNode):
            yield from walk(value)


def iter_child_nodes(node: ASTNode) -> Iterator[ASTNode]:
    """
    Yield all direct child nodes of node.
    Similar to Python's ast.iter_child_nodes().
    
    Args:
        node: The parent AST node
        
    Yields:
        Direct child nodes
    """
    for field_name in node._fields:
        value = getattr(node, field_name, None)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, ASTNode):
                    yield item
        elif isinstance(value, ASTNode):
            yield value
