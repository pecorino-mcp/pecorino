import importlib
from typing import Dict, Optional

import tree_sitter
import tree_sitter_language_pack as tslp

class TreeSitterGrammarManager:
    """
    Manages loading Tree-sitter grammars from tree-sitter-language-pack.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        # cache_dir is ignored, kept for backwards compatibility in the constructor
        self.loaded_languages: Dict[str, tree_sitter.Language] = {}

    def add_grammar(self, language: str, module_name: str, rev: Optional[str] = None):
        """
        No-op in the new system since packages are provided by tree-sitter-language-pack.
        """
        pass

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

        try:
            lang = tslp.get_language(language)
            self.loaded_languages[language] = lang
            return lang
        except Exception as e:
            raise ImportError(f"Parser for {language} is not installed or failed to load. Error: {e}")

