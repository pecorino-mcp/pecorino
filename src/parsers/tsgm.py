import importlib
from typing import Dict, Optional

import tree_sitter

# Default repository mapping for known languages
KNOWN_GRAMMARS = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "ruby": "tree_sitter_ruby",
    "swift": "tree_sitter_swift",
}

class TreeSitterGrammarManager:
    """
    Manages loading Tree-sitter grammars from individual Python packages.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        # cache_dir is ignored, kept for backwards compatibility in the constructor
        self.grammars = KNOWN_GRAMMARS.copy()
        self.loaded_languages: Dict[str, tree_sitter.Language] = {}

    def add_grammar(self, language: str, module_name: str, rev: Optional[str] = None):
        """
        Register a new grammar package.
        """
        self.grammars[language] = module_name

    def install(self, language: str):
        """
        No-op in the new system since packages are pre-compiled.
        """
        pass

    def build_all(self):
        """
        No-op in the new system since packages are pre-compiled.
        """
        pass

    def get_language(self, language: str) -> tree_sitter.Language:
        """
        Returns a tree_sitter.Language object for the specified language.
        """
        if language in self.loaded_languages:
            return self.loaded_languages[language]

        if language not in self.grammars:
            raise ValueError(f"Unknown language: {language}")

        module_name = self.grammars[language]
        try:
            module = importlib.import_module(module_name)

            # Dynamically handle packages with multiple grammars (e.g., typescript)
            if hasattr(module, "language"):
                lang_ptr = module.language()
            elif hasattr(module, f"language_{language}"):
                lang_ptr = getattr(module, f"language_{language}")()
            else:
                raise AttributeError(f"No language function found in {module_name}")

            lang = tree_sitter.Language(lang_ptr)
            self.loaded_languages[language] = lang
            return lang
        except ImportError:
            raise ImportError(f"Parser for {language} is not installed. Please run: pip install {module_name}")


