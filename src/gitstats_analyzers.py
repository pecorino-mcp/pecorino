"""
Analyzer functions for Gitstats3.

Contains functions for analyzing files, blobs, and SLOC that were previously
in the main module but caused circular dependencies when used by collectors.
"""

import re
from .gitstats_config import conf
from .gitstats_gitcommands import getpipeoutput
from .gitstats_helpers import should_include_file


def getnumoffilesfromrev(time_rev):
    """Get number of files changed in commit (filtered by allowed extensions)."""
    time_val, rev = time_rev
    if conf['filter_by_extensions']:
        try:
            all_files_output = getpipeoutput(['git ls-tree -r --name-only "%s"' % rev])
            if not all_files_output:
                return (int(time_val), rev, 0)
            all_files = all_files_output.split('\n')
            filtered_files = [f for f in all_files if f.strip() and should_include_file(f.split('/')[-1])]
            return (int(time_val), rev, len(filtered_files))
        except (ValueError, IndexError) as e:
            if conf['debug']:
                print(f'Warning: Failed to get file count for rev {rev}: {e}')
            return (int(time_val), rev, 0)
    else:
        try:
            output = getpipeoutput(['git ls-tree -r --name-only "%s"' % rev, 'wc -l'])
            if not output or not output.strip():
                return (int(time_val), rev, 0)
            count = int(output.split('\n')[0].strip())
            return (int(time_val), rev, count)
        except (ValueError, IndexError) as e:
            if conf['debug']:
                print(f'Warning: Failed to get file count for rev {rev}: {e}')
            return (int(time_val), rev, 0)


def getnumoflinesinblob(ext_blob):
    """Get number of lines in blob."""
    ext, blob_id = ext_blob
    try:
        output = getpipeoutput(['git cat-file blob %s' % blob_id, 'wc -l'])
        if not output or not output.strip():
            return (ext, blob_id, 0)
        count = int(output.split()[0])
        return (ext, blob_id, count)
    except (ValueError, IndexError) as e:
        if conf['debug']:
            print(f'Warning: Failed to get line count for blob {blob_id}: {e}')
        return (ext, blob_id, 0)


def analyzesloc(ext_blob):
    """
    Analyze source lines of code vs comments vs blank lines in a blob.
    Returns (ext, blob_id, total_lines, source_lines, comment_lines, blank_lines).
    """
    ext, blob_id = ext_blob
    content = getpipeoutput(['git cat-file blob %s' % blob_id])

    total_lines = 0
    source_lines = 0
    comment_lines = 0
    blank_lines = 0

    # Define comment patterns for different file types
    comment_patterns = {
        '.py': [r'^\s*#', r'^\s*"""', r"^\s*'''"],
        '.js': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.ts': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.java': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.cpp': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.c': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.h': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
        '.css': [r'^\s*/\*', r'^\s*\*'],
        '.html': [r'^\s*<!--', r'^\s*<!\-\-'],
        '.xml': [r'^\s*<!--', r'^\s*<!\-\-'],
        '.sh': [r'^\s*#'],
        '.rb': [r'^\s*#'],
        '.pl': [r'^\s*#'],
        '.php': [r'^\s*//', r'^\s*/\*', r'^\s*\*', r'^\s*#'],
    }

    patterns = comment_patterns.get(ext, [])

    for line in content.split('\n'):
        total_lines += 1
        line_stripped = line.strip()

        if not line_stripped:
            blank_lines += 1
        elif any(re.match(pattern, line) for pattern in patterns):
            comment_lines += 1
        else:
            source_lines += 1

    return (ext, blob_id, total_lines, source_lines, comment_lines, blank_lines)
