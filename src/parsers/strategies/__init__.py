from typing import Optional
from src.parsers.strategies.base_strategy import BaseParsingStrategy
from src.parsers.strategies.kotlin_strategy import KotlinStrategy
from src.parsers.strategies.csharp_strategy import CSharpStrategy
from src.parsers.strategies.rust_strategy import RustStrategy

class StrategyFactory:
    """
    Factory for obtaining the appropriate parsing strategy for a given language.
    """
    
    _strategies = {
        "kotlin": KotlinStrategy(),
        "c-sharp": CSharpStrategy(),
        "rust": RustStrategy(),
    }
    
    @classmethod
    def get_strategy(cls, language: str) -> Optional[BaseParsingStrategy]:
        """
        Return the language-specific strategy if one exists, otherwise None.
        If None is returned, the caller should fall back to the unified parsing logic.
        """
        return cls._strategies.get(language)
