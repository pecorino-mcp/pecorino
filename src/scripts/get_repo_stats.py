#!/usr/bin/env python3
"""
get_repo_stats.py

Print total commits and number of contributors for a git repository.

Usage:
  python scripts/get_repo_stats.py [--path PATH] [--json]

By default PATH is the current working directory. Outputs plain text or JSON.
This script uses the `git` command-line tool; no extra Python dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Tuple


def run_git_command(args: list[str], cwd: str) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def is_git_repo(path: str) -> bool:
    rc, _, _ = run_git_command(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return rc == 0


def get_total_commits(path: str) -> int:
    # Use rev-list --all --count for a fast commit count
    rc, out, err = run_git_command(["rev-list", "--all", "--count"], cwd=path)
    if rc != 0:
        raise RuntimeError(f"git rev-list failed: {err}")
    try:
        return int(out)
    except ValueError:
        raise RuntimeError(f"unexpected output from git rev-list: {out!r}")


def get_contributor_count(path: str) -> int:
    # Use shortlog to group by author. This counts unique author name+email lines.
    rc, out, err = run_git_command(["shortlog", "-sne", "--all"], cwd=path)
    if rc != 0:
        # Fallback: count unique author emails via git log
        rc2, out2, err2 = run_git_command(["log", "--format=%ae"], cwd=path)
        if rc2 != 0:
            raise RuntimeError(f"git shortlog/log failed: {err} / {err2}")
        emails = {line.strip() for line in out2.splitlines() if line.strip()}
        return len(emails)
    # Each non-empty line corresponds to a contributor
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return len(lines)


def find_git_repos(path: str, recursive: bool = False):
    """Yield git repository paths under `path`.

    If `path` itself is a git repo, yield it. Otherwise, if `recursive` is False,
    check immediate subdirectories for a `.git` directory or a valid git repo.
    If `recursive` is True, walk the tree and yield any directory that is a git
    repo (and do not descend into found repos).
    """
    # If the provided path itself is a git repo, just yield it.
    if is_git_repo(path):
        yield path
        return

    if not os.path.isdir(path):
        return

    if recursive:
        for dirpath, dirs, files in os.walk(path):
            # quick check for .git directory to avoid invoking git for every folder
            if os.path.isdir(os.path.join(dirpath, ".git")) or is_git_repo(dirpath):
                yield dirpath
                # don't descend into repositories
                dirs[:] = []
    else:
        with os.scandir(path) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                candidate = entry.path
                if os.path.isdir(os.path.join(candidate, ".git")) or is_git_repo(candidate):
                    yield candidate


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Report total commits and contributors for a git repo")
    parser.add_argument("--path", "-p", default=os.getcwd(), help="Path to the git repository (default: cwd)")
    parser.add_argument("--recursive", "-r", action="store_true", help="Recursively search for git repositories under PATH")
    args = parser.parse_args(argv)

    repo_path = os.path.abspath(args.path)

    if not os.path.exists(repo_path):
        print(f"Path does not exist: {repo_path}", file=sys.stderr)
        return 2

    # quick git check using the working directory (any existing path is fine)
    rc, _, err = run_git_command(["--version"], cwd=repo_path)
    if rc != 0:
        print(f"git command not found or not usable: {err}", file=sys.stderr)
        return 3

    results: list[dict] = []

    # If the path itself is a git repo, just report it. Otherwise, scan for repos
    # under the directory provided.
    repos_to_check = []
    if is_git_repo(repo_path):
        repos_to_check = [repo_path]
    elif os.path.isdir(repo_path):
        repos_to_check = list(find_git_repos(repo_path, recursive=args.recursive))
        if not repos_to_check:
            print(f"No git repositories found under: {repo_path}", file=sys.stderr)
            return 4
    else:
        print(f"Not a git repository or directory: {repo_path}", file=sys.stderr)
        return 4

    exit_code = 0
    for repo in repos_to_check:
        try:
            commits = get_total_commits(repo)
            contributors = get_contributor_count(repo)
            results.append({"path": repo, "total_commits": commits, "contributors": contributors})
        except RuntimeError as exc:
            print(f"Error processing {repo}: {exc}", file=sys.stderr)
            # continue processing other repos but set non-zero exit code
            exit_code = 5

    # Always output JSON
    if len(results) == 1:
        print(json.dumps(results[0], indent=2))
    else:
        print(json.dumps(results, indent=2))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
