import re
from typing import Dict, Any, List, Tuple

PREFIX_DENYLIST = {"m_", "_", "__", "g_", "s_", "k_"}
TYPE_PREFIXES = {"I", "C", "T", "E"}
SUFFIX_DENYLIST = {"_t", "_v", "_impl"}
VERB_SET = {
    "get", "fetch", "load", "query", "find", "read", "retrieve",
    "set", "put", "update", "patch",
    "create", "make", "new", "build", "construct", "init",
    "delete", "remove", "drop", "destroy", "clear",
    "is", "has", "can", "should", "will", "does",
    "save", "store", "write", "persist", "insert",
    "validate", "check", "ensure", "parse", "format"
}
STOP_WORDS = {"by", "with", "for", "of", "and", "or", "to", "in", "on"}

def strip_affixes(raw: str) -> Tuple[str, str, str, bool]:
    if not raw:
        return "", None, None, False

    is_magic = raw.startswith("__") and raw.endswith("__") and len(raw) > 4
    if is_magic:
        return raw[2:-2], "__", "__", True

    core = raw
    prefix = None
    suffix = None

    # Handle private marker without setting prefix
    m_leading = re.match(r'^(_+)(.*)', core)
    if m_leading:
        core = m_leading.group(2)

    # Prefix denylist
    for p in PREFIX_DENYLIST:
        if core.startswith(p):
            prefix = p
            core = core[len(p):]
            break

    # Type prefixes
    if not prefix and len(core) > 2 and core[0] in TYPE_PREFIXES and core[1].isupper():
        prefix = core[0]
        core = core[1:]

    # Suffix denylist
    for s in SUFFIX_DENYLIST:
        if core.endswith(s):
            suffix = s
            core = core[:-len(s)]
            break

    return core, prefix, suffix, is_magic

def detect_case_style(core: str) -> str:
    if not core:
        return "unknown"
    if "-" in core:
        return "kebab-case"
    if "_" in core:
        if core.isupper():
            return "SCREAMING_SNAKE_CASE"
        return "snake_case"
    if core[0].islower() and any(c.isupper() for c in core):
        return "camelCase"
    if core[0].isupper() and any(c.islower() for c in core):
        return "PascalCase"
    if core.isupper() and len(core) > 1:
        return "SCREAMING"
    if core.islower():
        return "lowercase"
    return "unknown"

def tokenize_identifier(core: str) -> List[str]:
    if not core:
        return []
    tmp = core.replace("-", "_")
    s1 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', tmp)
    s2 = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1)
    parts = [p for p in s2.split("_") if p]
    return [p.lower() for p in parts]

def analyze_grammar(tokens: List[str]) -> Dict[str, Any]:
    if not tokens:
        return {"verb": None, "entity": None, "qualifier": None}
    
    verb = tokens[0] if tokens[0] in VERB_SET else None
    start = 1 if verb else 0

    while start < len(tokens) and tokens[start] in STOP_WORDS:
        start += 1
    
    entity = tokens[start] if start < len(tokens) else None

    qual_tokens = tokens[start+1:] if entity else tokens[start:]
    qualifier = "_".join(qual_tokens) if qual_tokens else None

    return {"verb": verb, "entity": entity, "qualifier": qualifier}

def analyze_name(raw: str) -> Dict[str, Any]:
    if not raw:
        return {
            "tokens": [],
            "case_style": "unknown",
            "prefix": None,
            "suffix": None,
            "verb": None,
            "entity": None,
            "qualifier": None,
            "is_magic": False
        }
        
    core, prefix, suffix, is_magic = strip_affixes(raw)
    case_style = detect_case_style(core)
    tokens = tokenize_identifier(core)
    grammar = analyze_grammar(tokens)

    return {
        "tokens": tokens,
        "case_style": case_style,
        "prefix": prefix,
        "suffix": suffix,
        "verb": grammar["verb"],
        "entity": grammar["entity"],
        "qualifier": grammar["qualifier"],
        "is_magic": is_magic
    }
