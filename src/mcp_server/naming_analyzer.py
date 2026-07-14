import re
from typing import Dict, Any, List

def tokenize_identifier(name: str) -> List[str]:
    """
    Splits an identifier into semantic tokens based on typical programming boundaries.
    e.g. myHTTPServer2 -> ['my', 'HTTP', 'Server', '2']
    """
    if not name:
        return []
    tokens = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|[A-Z]+|\d+|[_\-\.]+', name)
    return [t for t in tokens if t]

def analyze_name(name: str) -> Dict[str, Any]:
    """
    Classifies a string into orthogonal naming properties.
    """
    result = {
        "case_style": "unknown",
        "prefix": "",
        "suffix": "",
        "is_magic": False
    }
    
    if not name or not isinstance(name, str):
        return result

    # 1. Magic methods / variables
    if name.startswith("__") and name.endswith("__") and len(name) > 4:
        result["is_magic"] = True
        result["prefix"] = "__"
        result["suffix"] = "__"
        core = name[2:-2]
    else:
        core = name
        # Extract leading underscores
        m_leading = re.match(r'^(_+)(.*)', core)
        if m_leading:
            result["prefix"] = m_leading.group(1)
            core = m_leading.group(2)
            
        # Extract trailing underscores
        m_trailing = re.match(r'^(.*?)(_+)$', core)
        if m_trailing:
            result["suffix"] = m_trailing.group(2)
            core = m_trailing.group(1)

        # Extract known Hungarian or C++ prefixes
        if not result["prefix"]:
            # e.g., m_name, s_name, g_name, t_name
            m_prefix = re.match(r'^([a-z]_)(.*)$', core)
            if m_prefix:
                result["prefix"] = m_prefix.group(1)
                core = m_prefix.group(2)
            else:
                # e.g., IService, CWidget, FVector, UObject, kConstant
                # Must be followed by a capital letter, so the remainder is PascalCase or camelCase
                m_cap_prefix = re.match(r'^([kICFUTA])([A-Z][a-z0-9].*)$', core)
                if m_cap_prefix:
                    result["prefix"] = m_cap_prefix.group(1)
                    core = m_cap_prefix.group(2)
                    
    # If there's nothing left after stripping prefixes, return flatcase
    if not core:
        result["case_style"] = "flatcase" if name.islower() else "UPPERFLATCASE" if name.isupper() else "mixed"
        return result

    # 2. Determine Case Style of the Core
    if re.match(r'^[a-z0-9]+$', core):
        result["case_style"] = "flatcase"
    elif re.match(r'^[A-Z0-9]+$', core):
        result["case_style"] = "UPPERFLATCASE"
    elif re.match(r'^[a-z][a-z0-9]*([A-Z][a-z0-9]+)*[A-Z]?$', core):
        result["case_style"] = "camelCase"
    elif re.match(r'^[A-Z][a-z0-9]*([A-Z][a-z0-9]+)*[A-Z]?$', core):
        result["case_style"] = "PascalCase"
    elif re.match(r'^[a-z0-9]+(_[a-z0-9]+)+$', core):
        result["case_style"] = "snake_case"
    elif re.match(r'^[A-Z0-9]+(_[A-Z0-9]+)+$', core):
        result["case_style"] = "SCREAMING_SNAKE_CASE"
    elif re.match(r'^[a-z0-9]+(-[a-z0-9]+)+$', core):
        result["case_style"] = "kebab-case"
    elif re.match(r'^[A-Z][a-z0-9]+(-[A-Z][a-z0-9]+)+$', core):
        result["case_style"] = "Train-Case"
    elif re.match(r'^[A-Z0-9]+(-[A-Z0-9]+)+$', core):
        result["case_style"] = "COBOL-CASE"
    elif re.match(r'^[a-z0-9]+(\.[a-z0-9]+)+$', core):
        result["case_style"] = "dot.case"
    else:
        result["case_style"] = "mixed"

    return result
