"""
Helper utility functions for Gitstats3.

Contains constants and utility functions extracted from the monolithic gitstats.py.
"""

import os
import platform
import re
import time
from typing import Dict, List, Any, Tuple

from .gitstats_config import get_config


# Platform detection
ON_LINUX = platform.system() == 'Linux'

# Timing variables
exectime_internal = 0.0
time_start = time.time()

# Day of week constants
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def getkeyssortedbyvalues(d: Dict[Any, Any]) -> List[Any]:
    """
    Get dictionary keys sorted by their values.
    
    Args:
        d: Dictionary to sort
        
    Returns:
        List of keys sorted by values in ascending order
    """
    return list(map(lambda el: el[1], sorted(map(lambda el: (el[1], el[0]), d.items()))))


def getkeyssortedbyvaluekey(d: Dict[str, Dict[str, Any]], key: str) -> List[str]:
    """
    Get dictionary keys sorted by a nested value key.
    
    Example:
        d = {'author1': {'commits': 512}, 'author2': {'commits': 100}}
        getkeyssortedbyvaluekey(d, 'commits') -> ['author2', 'author1']
    
    Args:
        d: Nested dictionary
        key: Key to sort by in the nested dict
        
    Returns:
        List of keys sorted by the nested value
    """
    return list(map(lambda el: el[1], sorted(map(lambda el: (d[el][key], el), d.keys()))))


def getstatsummarycounts(line: str) -> List[int]:
    """
    Parse git diff stat summary line to extract counts.
    
    Parses lines like "3 files changed, 10 insertions(+), 5 deletions(-)"
    
    Args:
        line: Git diff stat summary line
        
    Returns:
        List of [files_changed, insertions, deletions]
    """
    numbers = re.findall(r'\d+', line)
    if len(numbers) == 1:
        # Neither insertions nor deletions: may probably only happen for "0 files changed"
        numbers.append(0)
        numbers.append(0)
    elif len(numbers) == 2 and line.find('(+)') != -1:
        numbers.append(0)  # Only insertions were printed on line
    elif len(numbers) == 2 and line.find('(-)') != -1:
        numbers.insert(1, 0)  # Only deletions were printed on line
    return [int(n) for n in numbers]


def should_include_file(filename: str) -> bool:
    """
    Check if a file should be included in the analysis based on its extension.
    
    Args:
        filename: The filename to check
        
    Returns:
        True if the file should be included, False otherwise
    """
    config = get_config()
    
    if not config.filter_by_extensions:
        return True
    
    # Handle hidden files (starting with .)
    basename = os.path.basename(filename)
    if basename.startswith('.') and basename != '.':
        return False
    
    # Check if filename has an extension
    if filename.find('.') == -1:
        # No extension - include common extensionless files
        extensionless_includes = ['Makefile', 'Dockerfile', 'Rakefile', 'Gemfile', 'CMakeLists']
        return basename in extensionless_includes
    
    # Check multi-part extensions first (e.g., .d.ts, .spec.ts)
    filename_lower = filename.lower()
    for ext in config.allowed_extensions:
        if filename_lower.endswith(ext):
            return True
    
    return False


def format_duration(seconds: float) -> str:
    """
    Format a duration in seconds to a human-readable string.
    
    Args:
        seconds: Duration in seconds
        
    Returns:
        Formatted duration string (e.g., "1h 23m 45s")
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}h {minutes}m {secs:.0f}s"


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filename.
    
    Args:
        name: The string to sanitize
        
    Returns:
        Sanitized string safe for use as filename
    """
    # Replace problematic characters
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        name = name.replace(char, '_')
    return name


def parse_timestamp(timestamp_str: str) -> int:
    """
    Parse a Unix timestamp string to integer.
    
    Args:
        timestamp_str: Timestamp as string
        
    Returns:
        Timestamp as integer, or 0 if parsing fails
    """
    try:
        return int(timestamp_str)
    except (ValueError, TypeError):
        return 0


def get_output_format() -> str:
    """Return output format description."""
    return "HTML tables (no charts)"
