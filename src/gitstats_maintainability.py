"""
Maintainability Index calculator for Gitstats3.

Implements the Maintainability Index (MI) metric based on:
- Lines of Code (LOC)
- Halstead Volume
- McCabe Cyclomatic Complexity
"""

import math
import re
from typing import Dict, Any, Tuple, List, Optional


def calculate_maintainability_index(
    loc_metrics: Dict[str, int],
    halstead_metrics: Dict[str, float],
    mccabe_metrics: Dict[str, int]
) -> Dict[str, Any]:
    """
    Calculate Maintainability Index using LOC, Halstead, and McCabe metrics.
    
    Formula from software engineering literature:
    MIwoc = 171 - 5.2 * ln(aveV) - 0.23 * aveG - 16.2 * ln(aveLOC)
    MIcw = 50 * sin(√(2.4 * perCM))
    MI = MIwoc + MIcw
    
    Args:
        loc_metrics: Dictionary with LOCphy, LOCbl, LOCpro, LOCcom keys
        halstead_metrics: Dictionary with volume, difficulty, effort keys
        mccabe_metrics: Dictionary with complexity, decisions keys
        
    Returns:
        Dictionary with mi_woc, mi_cw, mi, and interpretation
    """
    # Get values with defaults
    loc_phy = loc_metrics.get('LOCphy', 1)
    loc_com = loc_metrics.get('LOCcom', 0)
    volume = halstead_metrics.get('volume', 1)
    complexity = mccabe_metrics.get('complexity', 1)
    
    # Avoid log(0) and division by zero
    loc_phy = max(1, loc_phy)
    volume = max(1, volume)
    complexity = max(1, complexity)
    
    # Calculate percent comments
    per_cm = (loc_com / loc_phy * 100) if loc_phy > 0 else 0
    
    # MI without comments
    mi_woc = 171 - 5.2 * math.log(volume) - 0.23 * complexity - 16.2 * math.log(loc_phy)
    
    # MI comment weight
    mi_cw = 50 * math.sin(math.sqrt(2.4 * per_cm))
    
    # Combined MI
    mi = mi_woc + mi_cw
    
    # Normalize to 0-100 scale (often done for readability)
    mi_normalized = max(0, min(100, mi))
    
    return {
        'mi_woc': round(mi_woc, 2),
        'mi_cw': round(mi_cw, 2),
        'mi': round(mi, 2),
        'mi_normalized': round(mi_normalized, 2),
        'interpretation': interpret_maintainability_index(mi)
    }


def interpret_maintainability_index(mi: float) -> str:
    """
    Interpret Maintainability Index score.
    
    Args:
        mi: Maintainability Index value
        
    Returns:
        String interpretation of the score
    """
    if mi >= 85:
        return "Excellent - Highly maintainable"
    elif mi >= 65:
        return "Good - Reasonably maintainable"
    elif mi >= 50:
        return "Moderate - Needs attention"
    elif mi >= 20:
        return "Poor - Difficult to maintain"
    else:
        return "Critical - Very difficult to maintain"


def calculate_loc_metrics(content: str, file_extension: str) -> Dict[str, int]:
    """
    Calculate Lines-of-Code metrics.
    
    Args:
        content: File content as string
        file_extension: File extension (e.g., '.py', '.java')
        
    Returns:
        Dictionary with LOCphy, LOCbl, LOCpro, LOCcom counts
    """
    lines = content.split('\n')
    loc_phy = len(lines)  # Physical lines
    loc_bl = 0  # Blank lines
    loc_com = 0  # Comment lines
    loc_pro = 0  # Program lines
    
    comment_patterns = _get_comment_patterns(file_extension)
    in_multi_line = False
    multi_start, multi_end = _get_multiline_comment_markers(file_extension)
    
    for line in lines:
        stripped = line.strip()
        
        # Blank line
        if not stripped:
            loc_bl += 1
            continue
        
        # Check for multi-line comment
        if multi_start and multi_start in stripped:
            in_multi_line = True
            loc_com += 1
            if multi_end and multi_end in stripped:
                in_multi_line = False
            continue
        
        if in_multi_line:
            loc_com += 1
            if multi_end and multi_end in stripped:
                in_multi_line = False
            continue
        
        # Single-line comment
        is_comment = False
        for pattern in comment_patterns:
            if re.match(pattern, stripped):
                is_comment = True
                loc_com += 1
                break
        
        if not is_comment:
            loc_pro += 1
    
    return {
        'LOCphy': loc_phy,
        'LOCbl': loc_bl,
        'LOCpro': loc_pro,
        'LOCcom': loc_com
    }


def _get_comment_patterns(file_extension: str) -> List[str]:
    """Get regex patterns for single-line comments based on file extension."""
    patterns = {
        '.py': [r'^#'],
        '.js': [r'^//'],
        '.ts': [r'^//'],
        '.jsx': [r'^//'],
        '.tsx': [r'^//'],
        '.java': [r'^//'],
        '.scala': [r'^//'],
        '.kt': [r'^//'],
        '.cpp': [r'^//'],
        '.c': [r'^//'],
        '.h': [r'^//'],
        '.hpp': [r'^//'],
        '.go': [r'^//'],
        '.rs': [r'^//'],
        '.swift': [r'^//'],
        '.rb': [r'^#'],
        '.sh': [r'^#'],
    }
    return patterns.get(file_extension.lower(), [r'^//'])


def _get_multiline_comment_markers(file_extension: str) -> Tuple[Optional[str], Optional[str]]:
    """Get multi-line comment markers for file extension."""
    if file_extension.lower() == '.py':
        # Python uses triple quotes but those are usually docstrings
        return ('"""', '"""')
    elif file_extension.lower() in ('.js', '.ts', '.jsx', '.tsx', '.java', '.scala', 
                                     '.kt', '.cpp', '.c', '.h', '.hpp', '.go', '.rs', '.swift'):
        return ('/*', '*/')
    return (None, None)


def calculate_halstead_metrics(content: str, file_extension: str) -> Dict[str, float]:
    """
    Calculate Halstead complexity metrics.
    
    Halstead metrics are based on counting operators and operands:
    - n1 = number of distinct operators
    - n2 = number of distinct operands
    - N1 = total number of operators
    - N2 = total number of operands
    
    Args:
        content: File content
        file_extension: File extension
        
    Returns:
        Dictionary with vocabulary, length, volume, difficulty, effort, time, bugs
    """
    operators, operands = _extract_operators_operands(content, file_extension)
    
    n1 = len(set(operators))  # Distinct operators
    n2 = len(set(operands))   # Distinct operands
    N1 = len(operators)       # Total operators
    N2 = len(operands)        # Total operands
    
    # Avoid division by zero
    if n1 == 0 or n2 == 0 or N1 == 0 or N2 == 0:
        return {
            'n1': 0, 'n2': 0, 'N1': 0, 'N2': 0,
            'vocabulary': 0, 'length': 0, 'volume': 0,
            'difficulty': 0, 'effort': 0, 'time': 0, 'bugs': 0
        }
    
    # Halstead metrics
    vocabulary = n1 + n2
    length = N1 + N2
    volume = length * math.log2(vocabulary) if vocabulary > 0 else 0
    difficulty = (n1 / 2) * (N2 / n2) if n2 > 0 else 0
    effort = difficulty * volume
    time_to_program = effort / 18  # Seconds
    estimated_bugs = volume / 3000
    
    return {
        'n1': n1, 'n2': n2, 'N1': N1, 'N2': N2,
        'vocabulary': vocabulary,
        'length': length,
        'volume': round(volume, 2),
        'difficulty': round(difficulty, 2),
        'effort': round(effort, 2),
        'time': round(time_to_program, 2),
        'bugs': round(estimated_bugs, 4)
    }


def _extract_operators_operands(content: str, file_extension: str) -> Tuple[List[str], List[str]]:
    """Extract operators and operands from source code."""
    # Simple regex-based extraction
    operators = []
    operands = []
    
    # Common operators for most languages
    operator_pattern = r'[\+\-\*/%=<>!&|^~\?:;,\.\[\]\{\}\(\)]|->|=>|==|!=|<=|>=|&&|\|\||<<|>>'
    
    # Operands are identifiers, numbers, strings
    operand_pattern = r'[a-zA-Z_][a-zA-Z0-9_]*|\"[^\"]*\"|\'[^\']*\'|\d+\.?\d*'
    
    # Remove comments and strings for cleaner parsing
    cleaned = _remove_comments_and_strings(content, file_extension)
    
    operators = re.findall(operator_pattern, cleaned)
    operands = re.findall(operand_pattern, cleaned)
    
    return operators, operands


def _remove_comments_and_strings(content: str, file_extension: str) -> str:
    """Remove comments and string literals from source code."""
    # Remove C-style multi-line comments
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    
    # Remove single-line comments
    if file_extension.lower() == '.py':
        content = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
    else:
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    
    # Remove strings (simple approach)
    content = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', content)
    content = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'", "''", content)
    
    return content


def calculate_mccabe_complexity(content: str, file_extension: str) -> Dict[str, int]:
    """
    Calculate McCabe Cyclomatic Complexity v(G).
    
    Formula: v(G) = #binaryDecision + 1
    Also: v(G) = #IFs + #LOOPs + 1
    
    Args:
        content: File content
        file_extension: File extension
        
    Returns:
        Dictionary with complexity and decisions count
    """
    # Remove comments and strings
    cleaned = _remove_comments_and_strings(content, file_extension)
    
    # Count decision points based on language
    decisions = 0
    
    # Common control flow keywords
    if file_extension.lower() == '.py':
        keywords = ['if', 'elif', 'for', 'while', 'except', 'with', 'and', 'or']
    elif file_extension.lower() in ('.go',):
        keywords = ['if', 'for', 'case', 'select', '&&', '||']
    elif file_extension.lower() in ('.rs',):
        keywords = ['if', 'else if', 'for', 'while', 'loop', 'match', '&&', '||']
    else:
        # C-style languages
        keywords = ['if', 'else if', 'for', 'while', 'case', 'catch', '&&', '||', '?']
    
    for keyword in keywords:
        # Use word boundary to avoid matching inside other words
        if keyword in ('&&', '||', '?'):
            pattern = re.escape(keyword)
        else:
            pattern = r'\b' + keyword + r'\b'
        matches = re.findall(pattern, cleaned)
        decisions += len(matches)
    
    complexity = decisions + 1
    
    return {
        'complexity': complexity,
        'decisions': decisions
    }
