from abc import ABC, abstractmethod
from typing import List, Optional

from src.parsers.ast import ASTNode


class BaseParsingStrategy(ABC):
    """
    Abstract base class for language-specific parsing strategies.
    
    Strategies are responsible for identifying language-specific constructs
    (like Kotlin data classes or Rust traits) and mapping them into the 
    unified AST structure.
    """

    @abstractmethod
    def get_language(self) -> str:
        """Return the language name (e.g., 'kotlin', 'c-sharp', 'rust')."""
        pass

    @abstractmethod
    def extract_classes(self, tree, source_bytes: bytes, filepath: str) -> List[ASTNode]:
        """Extract classes, interfaces, and similar constructs from the tree."""
        pass

    @abstractmethod
    def extract_functions(self, tree, source_bytes: bytes, filepath: str, parent: Optional[ASTNode] = None) -> List[ASTNode]:
        """Extract functions, methods, and similar constructs from the tree."""
        pass

    def _extract_text(self, node, source_bytes: bytes) -> str:
        """Helper to extract text from a node."""
        if node is None:
            return ""
        return source_bytes[node.start_byte:node.end_byte].decode('utf8', errors='ignore')

    def _get_docstring(self, node, source_bytes: bytes) -> str:
        """
        Helper to extract docstrings that appear immediately before a node.
        Can be overridden by specific strategies if comment structures differ.
        """
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ['comment', 'line_comment', 'block_comment']:
            comments.insert(0, self._extract_text(prev, source_bytes))
            prev = prev.prev_sibling
        if comments:
            docstring = "\n".join(comments).strip()
        return docstring
