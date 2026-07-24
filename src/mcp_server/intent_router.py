import re
from typing import Optional, Tuple

class IntentRouter:
    """
    Heuristic classifier that routes natural language search queries
    to the appropriate search mode.
    """
    
    def __init__(self):
        # Ordered list of heuristics: (regex_pattern, mode, extracted_intent)
        self.rules = [
            # Callers
            (re.compile(r"^(?:who|what)\s+calls\s+([a-zA-Z0-9_.]+)\??$", re.IGNORECASE), "callers", None),
            (re.compile(r"^callers\s+of\s+([a-zA-Z0-9_.]+)$", re.IGNORECASE), "callers", None),
            
            # Callees
            (re.compile(r"^(?:what|who)\s+does\s+([a-zA-Z0-9_.]+)\s+call\??$", re.IGNORECASE), "callees", None),
            (re.compile(r"^callees\s+of\s+([a-zA-Z0-9_.]+)$", re.IGNORECASE), "callees", None),
            
            # Impact
            (re.compile(r"^(?:impact|dependencies)\s+of\s+([a-zA-Z0-9_.\/]+)$", re.IGNORECASE), "impact", None),
            (re.compile(r"^what\s+depends\s+on\s+([a-zA-Z0-9_.\/]+)\??$", re.IGNORECASE), "impact", None),
            
            # Community / Semantic Neighborhood
            (re.compile(r"^(?:community|neighborhood|related\s+to)\s+([a-zA-Z0-9_.]+)$", re.IGNORECASE), "community", None),
            
            # Intents (Dead Code)
            (re.compile(r"^(?:dead|unused)\s+code$", re.IGNORECASE), "intent", "dead_code"),
            
            # Intents (Entry Points)
            (re.compile(r"^entry\s*points?$", re.IGNORECASE), "intent", "entry_points"),
            
            # Intents (All Classes / Functions)
            (re.compile(r"^(?:all|list)\s+classes$", re.IGNORECASE), "intent", "all_classes"),
            (re.compile(r"^(?:all|list)\s+functions$", re.IGNORECASE), "intent", "all_functions"),
            
            # Cypher
            (re.compile(r"^MATCH\s*\(.*\).*RETURN", re.IGNORECASE | re.DOTALL), "cypher", None),
        ]

    def route(self, query: str) -> Tuple[str, str, Optional[str]]:
        """
        Routes the query.
        Returns: (mode, updated_query, intent)
        """
        if not query:
            return "hybrid", "", None
            
        q = query.strip()
        
        for pattern, mode, intent_str in self.rules:
            match = pattern.match(q)
            if match:
                # If there is a capture group, we extract it as the new query symbol
                extracted_query = match.group(1) if match.groups() else q
                return mode, extracted_query, intent_str
                
        # Fallback to default
        return "hybrid", q, None
