import json
import logging
import os
import pathlib
from typing import Callable, Optional, Set, Tuple

logger = logging.getLogger(__name__)

IGNORED_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".tox",
    "build",
    "dist",
    "third_party",
    "dataset",
    "build_test",
    "build-context",
    "modules",
}


def walk_repo_files(
    root_path: str | pathlib.Path,
    supported_extensions: Optional[Set[str]] = None,
) -> list[pathlib.Path]:
    """Walk a repository directory, pruning ignored directories and optionally filtering by extension."""
    path = pathlib.Path(root_path).resolve()
    files: list[pathlib.Path] = []
    for r, d, fnames in os.walk(str(path)):
        d[:] = [
            dirname
            for dirname in d
            if dirname not in IGNORED_DIRS
            and not dirname.startswith(".")
            and not dirname.endswith("-codeql-db")
            and not dirname.endswith(".egg-info")
        ]
        for fname in fnames:
            fp = (pathlib.Path(r) / fname).resolve()
            if supported_extensions is None or fp.suffix in supported_extensions:
                files.append(fp)
    return files


def build_repo_file_cache(repo_path: str) -> Tuple[list[str], dict[str, list[str]]]:
    """Build a list of all repository file paths and a basename -> [full_paths] lookup table."""
    repo_files_cache: list[str] = []
    repo_basename_index: dict[str, list[str]] = {}
    for fp in walk_repo_files(repo_path, supported_extensions=None):
        full = str(fp)
        repo_files_cache.append(full)
        repo_basename_index.setdefault(fp.name, []).append(full)
    return repo_files_cache, repo_basename_index


def find_file_in_repo(
    dep_string: str,
    repo_path: str,
    repo_basename_index: Optional[dict[str, list[str]]] = None,
) -> str:
    """Find a file in the repo that matches dep_string (e.g. ending with it)."""
    if repo_basename_index is None:
        _, repo_basename_index = build_repo_file_cache(repo_path)

    norm_dep = dep_string.replace('\\', '/').lstrip('/')
    basename = os.path.basename(norm_dep)

    # O(1) lookup by basename, then filter by suffix match
    candidates = repo_basename_index.get(basename, [])
    for filepath in candidates:
        normed = filepath.replace('\\', '/')
        if normed.endswith('/' + norm_dep) or normed.endswith('/' + dep_string):
            return filepath
    # If we got candidates but no suffix match, return the first one
    if candidates:
        return candidates[0]
    return ""


def resolve_relative_fallback(dep_string: str, source_filepath: str) -> str:
    """Shared fallback: resolve a relative dep_string against the source file's directory."""
    return os.path.abspath(os.path.join(os.path.dirname(source_filepath), dep_string))


def resolve_dependency(
    dep_string: str,
    source_filepath: str,
    file_extension: str,
    repo_path: str,
    find_file_fn: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """Enhanced language-specific dependency file resolution.

    Returns the resolved absolute file path if found in the repository, or None if unresolved.
    """
    if not dep_string:
        return None

    def _find_in_repo(target: str) -> str:
        if find_file_fn is not None:
            return find_file_fn(target)
        return find_file_in_repo(target, repo_path)

    # 1. JS/TS: Relative paths, node_modules, package.json resolution
    if file_extension in ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']:
        exts = ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']
        if dep_string.startswith('.'):
            base_dir = os.path.dirname(source_filepath)
            target_base = os.path.join(base_dir, dep_string)
            if os.path.isfile(target_base):
                return os.path.abspath(target_base)
            for ext in exts:
                if os.path.exists(target_base + ext):
                    return os.path.abspath(target_base + ext)
            if os.path.isdir(target_base):
                for ext in exts:
                    idx_path = os.path.join(target_base, f'index{ext}')
                    if os.path.exists(idx_path):
                        return os.path.abspath(idx_path)
            fallback = resolve_relative_fallback(dep_string, source_filepath)
            return fallback if os.path.exists(fallback) else None
        else:
            curr_dir = os.path.dirname(source_filepath)
            while True:
                nm_path = os.path.join(curr_dir, 'node_modules', dep_string)
                if os.path.exists(nm_path):
                    if os.path.isfile(nm_path):
                        return os.path.abspath(nm_path)
                    if os.path.isdir(nm_path):
                        pkg_json = os.path.join(nm_path, 'package.json')
                        if os.path.exists(pkg_json):
                            try:
                                with open(pkg_json, encoding='utf-8') as f:
                                    pkg_data = json.load(f)
                                    main_file = pkg_data.get('main')
                                    if main_file:
                                        cand = os.path.abspath(os.path.join(nm_path, main_file))
                                        if os.path.exists(cand):
                                            return cand
                                        for ext in exts:
                                            if os.path.exists(cand + ext):
                                                return cand + ext
                                    exports = pkg_data.get('exports')
                                    if exports:
                                        if isinstance(exports, str):
                                            cand = os.path.abspath(os.path.join(nm_path, exports))
                                            if os.path.exists(cand):
                                                return cand
                                        elif isinstance(exports, dict) and isinstance(exports.get('.'), str):
                                            cand = os.path.abspath(os.path.join(nm_path, exports['.']))
                                            if os.path.exists(cand):
                                                return cand
                            except Exception as e:
                                logger.debug("Failed to parse package.json: %s", e)
                        for ext in exts:
                            cand = os.path.join(nm_path, f'index{ext}')
                            if os.path.exists(cand):
                                return os.path.abspath(cand)
                if curr_dir == repo_path or curr_dir == os.path.dirname(curr_dir):
                    break
                curr_dir = os.path.dirname(curr_dir)
            return None

    # 2. C/C++: Include paths
    elif file_extension in ['.cpp', '.cc', '.cxx', '.h', '.hpp', '.c']:
        base_dir = os.path.dirname(source_filepath)
        local_test = os.path.abspath(os.path.join(base_dir, dep_string))
        if os.path.exists(local_test):
            return local_test
        repo_test = os.path.abspath(os.path.join(repo_path, dep_string))
        if os.path.exists(repo_test):
            return repo_test
        for inc in ['', 'include', 'src', 'src/include']:
            test_path = os.path.abspath(os.path.join(repo_path, inc, dep_string))
            if os.path.exists(test_path):
                return test_path
        found = _find_in_repo(dep_string)
        if found:
            return found
        if dep_string.startswith('.'):
            fallback = resolve_relative_fallback(dep_string, source_filepath)
            if os.path.exists(fallback):
                return fallback
        return None

    # 3. Python: relative imports (count dots) or absolute
    elif file_extension in ['.py', '.pyi']:
        if dep_string.startswith('.'):
            dots = len(dep_string) - len(dep_string.lstrip('.'))
            dep_without_dots = dep_string[dots:]
            base_dir = os.path.dirname(source_filepath)
            for _ in range(dots - 1):
                base_dir = os.path.dirname(base_dir)
            parts = [p for p in dep_without_dots.split('.') if p] if dep_without_dots else []
            search_bases = [base_dir]
        else:
            parts = [p for p in dep_string.split('.') if p]
            search_bases = [repo_path, os.path.dirname(source_filepath)]

        for base in search_bases:
            target_path = os.path.join(base, *parts) if parts else base
            if os.path.exists(target_path + '.py'):
                return os.path.abspath(target_path + '.py')
            if os.path.exists(target_path + '.pyi'):
                return os.path.abspath(target_path + '.pyi')
            if os.path.isdir(target_path):
                init_path = os.path.join(target_path, '__init__.py')
                if os.path.exists(init_path):
                    return os.path.abspath(init_path)
            if len(parts) > 1:
                parent_target = os.path.join(base, *parts[:-1])
                if os.path.exists(parent_target + '.py'):
                    return os.path.abspath(parent_target + '.py')
        return None

    # 4. Go
    elif file_extension == '.go':
        module_name = ""
        go_mod = os.path.join(repo_path, 'go.mod')
        if os.path.exists(go_mod):
            try:
                with open(go_mod, encoding='utf-8') as f:
                    for line in f:
                        if line.strip().startswith('module '):
                            module_name = line.strip().split()[1]
                            break
            except Exception as e:
                logger.debug("Failed to read go.mod: %s", e)
        if module_name and dep_string.startswith(module_name):
            relative_path = dep_string[len(module_name):].lstrip('/')
            target_path = os.path.abspath(os.path.join(repo_path, relative_path))
            if os.path.exists(target_path):
                return target_path
            if os.path.exists(target_path + '.go'):
                return target_path + '.go'

        target_path = os.path.abspath(os.path.join(repo_path, dep_string))
        if os.path.exists(target_path):
            return target_path
        found = _find_in_repo(dep_string)
        if found:
            return found
        if dep_string.startswith('.'):
            fallback = resolve_relative_fallback(dep_string, source_filepath)
            if os.path.exists(fallback):
                return fallback
        return None

    # 5. Rust
    elif file_extension == '.rs':
        parts = [p for p in dep_string.split('::') if p and p not in ('crate', 'self', 'super')]
        if parts:
            target_path = os.path.join(repo_path, *parts)
            if os.path.exists(target_path + '.rs'):
                return os.path.abspath(target_path + '.rs')
            if os.path.isdir(target_path):
                for mod_file in ['mod.rs', 'lib.rs', 'main.rs']:
                    mod_path = os.path.join(target_path, mod_file)
                    if os.path.exists(mod_path):
                        return os.path.abspath(mod_path)
            found = _find_in_repo(parts[-1] + '.rs')
            if found:
                return found
        if dep_string.startswith('.'):
            fallback = resolve_relative_fallback(dep_string, source_filepath)
            if os.path.exists(fallback):
                return fallback
        return None

    # 6. Fallback
    else:
        if dep_string.startswith('.'):
            fallback = resolve_relative_fallback(dep_string, source_filepath)
            return fallback if os.path.exists(fallback) else None
        parts = dep_string.split('.')
        dep_suffix = "/".join(parts)
        for ext in ['.py', '.java', '.js', '.ts', '.go', '.rs', '.swift']:
            test_abs = os.path.join(repo_path, dep_suffix + ext)
            if os.path.exists(test_abs):
                return os.path.abspath(test_abs)
        return None
