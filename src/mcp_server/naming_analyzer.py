import re
from typing import Dict, Any, List, Tuple

PREFIX_DENYLIST = {"m_", "_", "__", "g_", "s_", "k_"}
TYPE_PREFIXES = {"I", "C", "T", "E", "k"}
SUFFIX_DENYLIST = {"_t", "_v", "_impl", "_"}
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
        return "", "", "", False

    is_magic = raw.startswith("__") and raw.endswith("__") and len(raw) > 4
    if is_magic:
        return raw[2:-2], "__", "__", True

    core = raw
    prefix = ""
    suffix = ""

    # Prefix denylist
    for p in sorted(PREFIX_DENYLIST, key=len, reverse=True):
        if core.startswith(p):
            prefix = p
            core = core[len(p):]
            break

    # Type prefixes
    if not prefix and len(core) > 2 and core[0] in TYPE_PREFIXES and core[1].isupper():
        prefix = core[0]
        core = core[1:]

    # Suffix denylist
    for s in sorted(SUFFIX_DENYLIST, key=len, reverse=True):
        if core.endswith(s):
            suffix = s
            core = core[:-len(s)]
            break

    return core, prefix, suffix, is_magic

def detect_case_style(core: str) -> str:
    if not core:
        return "unknown"
    if "." in core:
        return "dot.case"
    if "-" in core:
        if core.isupper():
            return "COBOL-CASE"
        if core.istitle() or (core[0].isupper() and any(c.isupper() for c in core[1:])):
            return "Train-Case"
        return "kebab-case"
    if "_" in core:
        if core.isupper():
            return "SCREAMING_SNAKE_CASE"
        return "snake_case"
    if core[0].islower() and any(c.isupper() for c in core):
        return "camelCase"
    if core[0].isupper() and any(c.islower() for c in core):
        return "PascalCase"
    if core.isupper():
        return "UPPERFLATCASE"
    if core.islower():
        return "flatcase"
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

def analyze_name(raw: str, file_path: str = "") -> Dict[str, Any]:
    if not raw:
        return {
            "tokens": [],
            "case_style": "unknown",
            "prefix": "",
            "suffix": "",
            "verb": None,
            "entity": None,
            "qualifier": None,
            "is_magic": False,
            "canonical_verb": "unknown",
            "canonical_entity": "unknown",
            "domain": "unknown",
            "intent": "unknown"
        }
        
    core, prefix, suffix, is_magic = strip_affixes(raw)
    case_style = detect_case_style(core)
    tokens = tokenize_identifier(core)
    grammar = analyze_grammar(tokens)
    c_verb = canonicalize_verb(tokens)
    c_entity = canonicalize_entity(tokens)
    domain = infer_domain(file_path) if file_path else "unknown"
    intent = infer_intent(c_verb)

    return {
        "tokens": tokens,
        "case_style": case_style,
        "prefix": prefix,
        "suffix": suffix,
        "verb": grammar["verb"],
        "entity": grammar["entity"],
        "qualifier": grammar["qualifier"],
        "is_magic": is_magic,
        "canonical_verb": c_verb,
        "canonical_entity": c_entity,
        "domain": domain,
        "intent": intent
    }

VERB_SYNONYMS = {
    # retrieve
    "get": "retrieve", "fetch": "retrieve", "load": "retrieve",
    "query": "retrieve", "find": "retrieve", "read": "retrieve", "select": "retrieve",
    # persist
    "save": "persist", "store": "persist", "write": "persist",
    "persist": "persist", "insert": "persist", "upsert": "persist",
    # update, create, delete, check
    "set": "update", "put": "update", "update": "update", "patch": "update",
    "create": "create", "make": "create", "build": "create", "new": "create",
    "delete": "delete", "remove": "delete", "drop": "delete", "clear": "delete",
    "destroy": "delete",
    "is": "check", "has": "check", "can": "check", "should": "check",
    "validate": "check", "verify": "check",
}

INTENT_MAP = {
    'retrieve': 'query',
    'persist': 'mutation',
    'create': 'mutation',
    'update': 'mutation',
    'delete': 'mutation',
    'check': 'validation',
    'validate': 'validation'
}

def canonicalize_verb(tokens: List[str]) -> str:
    for t in tokens:
        if t in VERB_SYNONYMS:
            return VERB_SYNONYMS[t]
    return "unknown"

def canonicalize_entity(tokens: List[str]) -> str:
    # Filter out verbs, stop words, and common noisy trailing tokens
    noisy = {"id", "type", "status", "state", "list", "array"}
    filtered = [t for t in tokens if t not in VERB_SYNONYMS and t not in STOP_WORDS and t not in noisy]
    
    if not filtered:
        # Fallback to just non-verbs if filtering removed everything
        filtered = [t for t in tokens if t not in VERB_SYNONYMS]
    
    if not filtered: return "unknown"
    
    # take first meaningful token (which is usually the entity in get_user_by_id)
    # wait, if it's UserRepository.insert -> user, repository -> repository
    # Actually let's use the first non-verb token as entity
    entity = filtered[0].lower()
    if entity.endswith('s') and len(entity) > 3 and not entity.endswith('ss'):
        entity = entity[:-1]
    entity = re.sub(r'(info|data|record|model|repository|service|controller|handler)$', '', entity)
    return entity or "unknown"

def infer_domain(file_path: str) -> str:
    p = file_path.lower()
    if "/auth" in p or "auth" in p.split("/")[-1]: return "auth"
    if "/payment" in p or "/pay" in p or "/billing" in p: return "payment"
    if "/user" in p or "/account" in p: return "user"
    if "/infra" in p or "/config" in p: return "infra"
    return "core"

def infer_intent(canonical_verb: str) -> str:
    return INTENT_MAP.get(canonical_verb, 'operation')
