"""
String resources loader for Gitstats3.

Loads user-facing text from gitstats_strings.json for centralized string management.
Access strings via the S dictionary using dot-notation keys: S['category.key']

Example:
    from .gitstats_strings import S
    print(S['error.scan_folder_not_exist'].format(path='/some/path'))
"""

import json
import os

_strings = {}


def _load_strings():
    """Load strings from JSON file, flattening nested keys with dot notation."""
    global _strings
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gitstats_strings.json')
    with open(json_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    for category, entries in raw.items():
        if category.startswith('_'):
            continue
        if isinstance(entries, dict):
            for key, value in entries.items():
                _strings[f'{category}.{key}'] = value
        else:
            _strings[category] = entries


# Load on import
_load_strings()

# Public accessor — use S['category.key'] to get strings
S = _strings
