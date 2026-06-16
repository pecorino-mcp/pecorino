"""
Git command execution utilities for Gitstats3.

Provides safe wrappers for executing git commands via subprocess.
"""

import os
import subprocess
import time
import threading
from typing import List, Optional, Tuple

from .gitstats_config import get_config
from .gitstats_helpers import ON_LINUX


# Thread-safe tracking of external execution time
_exectime_external = 0.0
_exectime_lock = threading.Lock()


def get_exectime_external() -> float:
    """Get the total external command execution time."""
    return _exectime_external


def reset_exectime_external() -> None:
    """Reset the external execution time counter."""
    global _exectime_external
    with _exectime_lock:
        _exectime_external = 0.0


def getpipeoutput(cmds: List[str], quiet: bool = False) -> str:
    """
    Execute a pipeline of shell commands and return output.
    
    Args:
        cmds: List of shell command strings to pipe together
        quiet: If True, suppress progress output
        
    Returns:
        Command output as string (decoded UTF-8)
        
    Raises:
        TypeError: If commands are not strings
    """
    global _exectime_external
    config = get_config()
    start = time.time()
    
    # Basic input validation to prevent command injection
    for cmd in cmds:
        if not isinstance(cmd, str):
            raise TypeError("Commands must be strings")
        # Check for obvious command injection attempts
        if any(dangerous in cmd for dangerous in [';', '&&', '||', '`', '$(']):
            print(f'Warning: Potentially dangerous command detected: {cmd}')
    
    if (not quiet and ON_LINUX and os.isatty(1)) or config.verbose:
        print('>> ' + ' | '.join(cmds), end='')
        import sys
        sys.stdout.flush()
    
    p = subprocess.Popen(cmds[0], stdout=subprocess.PIPE, shell=True)
    processes = [p]
    for x in cmds[1:]:
        p = subprocess.Popen(x, stdin=p.stdout, stdout=subprocess.PIPE, shell=True)
        processes.append(p)
    
    output = p.communicate()[0]
    for proc in processes:
        proc.wait()
    
    end = time.time()
    if not quiet or config.verbose or config.debug:
        if ON_LINUX and os.isatty(1):
            print('\r', end='')
        print('[%.5f] >> %s' % (end - start, ' | '.join(cmds)))
    
    if config.debug:
        print(f'DEBUG: Command output ({len(output)} bytes): {output[:200].decode("utf-8", errors="replace")}...')
    
    with _exectime_lock:
        _exectime_external += (end - start)
    
    return output.decode('utf-8', errors='replace').rstrip('\n')


def getpipeoutput_list(cmd_list: List[str], quiet: bool = False) -> str:
    """
    Execute command list without shell interpretation for safer path handling.
    
    Args:
        cmd_list: Command as list of arguments (not shell-interpreted)
        quiet: If True, suppress progress output
        
    Returns:
        Command output as string, empty string on error
    """
    global _exectime_external
    config = get_config()
    start = time.time()
    
    if (not quiet and ON_LINUX and os.isatty(1)) or config.verbose:
        print('>> ' + ' '.join(cmd_list), end='')
        import sys
        sys.stdout.flush()
    
    try:
        p = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = p.communicate()
        if p.returncode != 0:
            if not quiet:
                print(f'\nCommand failed: {" ".join(cmd_list)}')
                print(f'Error: {error.decode("utf-8", errors="replace")}')
            return ''
    except Exception as e:
        if not quiet:
            print(f'\nCommand execution failed: {e}')
        return ''
    
    end = time.time()
    if not quiet or config.verbose or config.debug:
        if ON_LINUX and os.isatty(1):
            print('\r', end='')
        print('[%.5f] >> %s' % (end - start, ' '.join(cmd_list)))
    
    if config.debug:
        print(f'DEBUG: Command output ({len(output)} bytes): {output[:200].decode("utf-8", errors="replace")}...')
    
    with _exectime_lock:
        _exectime_external += (end - start)
    
    return output.decode('utf-8', errors='replace').rstrip('\n')


def get_default_branch() -> str:
    """
    Get the default branch name from git configuration or detect it.
    
    Returns:
        Default branch name (e.g., 'main', 'master')
    """
    # First try to get the default branch from git config
    try:
        default_branch = getpipeoutput(['git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null']).strip()
        if default_branch:
            # Extract branch name from refs/remotes/origin/HEAD -> refs/remotes/origin/main
            branch = default_branch.replace('refs/remotes/origin/', '')
            if branch:
                return branch
    except Exception:
        pass
    
    # Try to get from current HEAD if in a repository
    try:
        current_branch = getpipeoutput(['git rev-parse --abbrev-ref HEAD 2>/dev/null']).strip()
        if current_branch and current_branch != 'HEAD':
            return current_branch
    except Exception:
        pass
    
    # Try to get from git config init.defaultBranch
    try:
        default_branch = getpipeoutput(['git config --get init.defaultBranch 2>/dev/null']).strip()
        if default_branch:
            return default_branch
    except Exception:
        pass
    
    # Try common main branch names in order of preference
    main_branch_candidates = ['main', 'master', 'develop', 'development']
    
    # Get all local branches
    try:
        branches_output = getpipeoutput(['git branch 2>/dev/null'])
        if branches_output:
            local_branches = [line.strip().lstrip('* ') for line in branches_output.split('\n') if line.strip()]
            
            # Check if any of the common main branches exist
            for candidate in main_branch_candidates:
                if candidate in local_branches:
                    return candidate
            
            # If none found and we have branches, use the first branch
            if local_branches:
                return local_branches[0]
    except Exception:
        pass
    
    # Fall back to master
    return 'master'


def get_first_parent_flag() -> str:
    """
    Get --first-parent flag if scanning only default branch.
    
    Returns:
        '--first-parent' or empty string based on config
    """
    config = get_config()
    return '--first-parent' if config.scan_default_branch_only else ''


def getlogrange(defaultrange: str = 'HEAD', end_only: bool = True) -> str:
    """
    Get the log range with optional date filtering.
    
    Args:
        defaultrange: Default commit range
        end_only: If True, only use end commit
        
    Returns:
        Git log range string
    """
    config = get_config()
    commit_range = getcommitrange(defaultrange, end_only)
    if len(config.start_date) > 0:
        return '--since="%s" "%s"' % (config.start_date, commit_range)
    return commit_range


def getcommitrange(defaultrange: str = 'HEAD', end_only: bool = False) -> str:
    """
    Get the commit range based on configuration.
    
    Args:
        defaultrange: Default commit range to use
        end_only: If True, only return end commit
        
    Returns:
        Git commit range string
    """
    config = get_config()
    
    if len(config.commit_end) > 0:
        if end_only or len(config.commit_begin) == 0:
            return config.commit_end
        return '%s..%s' % (config.commit_begin, config.commit_end)
    
    # If configured to scan only default branch and using default range
    if config.scan_default_branch_only and defaultrange == 'HEAD':
        default_branch = get_default_branch()
        if config.verbose:
            print(f'Scanning only default branch: {default_branch}')
        return default_branch
    
    return defaultrange


def is_git_repository(path: str) -> bool:
    """
    Check if the given path is a valid Git repository.
    
    Args:
        path: Path to check
        
    Returns:
        True if the path is a valid Git repository
    """
    if not os.path.exists(path) or not os.path.isdir(path):
        return False
    
    # Check if .git directory exists
    git_dir = os.path.join(path, '.git')
    if os.path.exists(git_dir):
        # For regular repositories, .git should be a directory
        # For worktrees, .git might be a file pointing to the real .git directory
        if os.path.isdir(git_dir) or os.path.isfile(git_dir):
            return True
    
    # Also check if we're inside a git repository
    try:
        current_dir = os.getcwd()
        try:
            os.chdir(path)
            result = subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        finally:
            os.chdir(current_dir)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return False
    
    return False


def getversion() -> str:
    """
    Get the gitstats version from git.
    
    Returns:
        Version string or 'unknown'
    """
    try:
        import os.path
        gitstats_repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cmd = [
            'git', f'--git-dir={gitstats_repo}/.git', f'--work-tree={gitstats_repo}',
            'rev-parse', '--short', 'HEAD'
        ]
        return getpipeoutput_list(cmd)
    except Exception:
        return 'unknown'


def getgitversion() -> str:
    """
    Get the installed git version.
    
    Returns:
        Git version string
    """
    return getpipeoutput(['git --version']).split('\n')[0]
