import logging
import math
import os
import subprocess
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


def compute_git_features(dirpath: str) -> list:
    """Parse git log to extract per-file temporal and stable features.

    Features extracted per file:
    - git_commit_count: total commits touching file
    - git_days_since_change: days since last modification
    - git_survival_days: days since first commit / creation
    - git_churn: total lines added + deleted across commits
    - git_authors: number of unique commit authors
    - git_ownership_entropy: Shannon entropy of author commit distribution
    - git_bug_fix_ratio: fraction of commits with bug-fix keywords
    - git_rename_count: number of rename events involving file
    """
    try:
        cmd = ["git", "log", "--pretty=format:COMMIT||%at||%an||%s", "--numstat"]
        proc = subprocess.Popen(
            cmd,
            cwd=dirpath,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        stdout, _ = proc.communicate()
        if proc.returncode != 0 or not stdout:
            return []
    except Exception as e:
        logger.debug("Failed to run git log for git features: %s", e)
        return []

    now_ts = time.time()
    bug_keywords = ("fix", "bug", "issue", "patch", "resolve", "close")

    file_commit_counts: dict[str, int] = defaultdict(int)
    file_bug_fix_commits: dict[str, int] = defaultdict(int)
    file_author_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    file_churn: dict[str, int] = defaultdict(int)
    file_latest_ts: dict[str, int] = {}
    file_first_ts: dict[str, int] = {}

    current_ts = None
    current_author = None
    current_is_bug = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT||"):
            parts = line.split("||", 3)
            if len(parts) >= 4:
                try:
                    current_ts = int(parts[1])
                except ValueError:
                    current_ts = int(now_ts)
                current_author = parts[2].strip()
                subject = parts[3].lower()
                current_is_bug = any(k in subject for k in bug_keywords)
        elif current_ts is not None and "\t" in line:
            tokens = line.split("\t")
            if len(tokens) == 3:
                added_str, deleted_str, rel_path = tokens
                abs_path = os.path.abspath(os.path.join(dirpath, rel_path))

                file_commit_counts[abs_path] += 1
                if current_is_bug:
                    file_bug_fix_commits[abs_path] += 1
                if current_author:
                    file_author_counts[abs_path][current_author] += 1

                added = int(added_str) if added_str.isdigit() else 0
                deleted = int(deleted_str) if deleted_str.isdigit() else 0
                file_churn[abs_path] += added + deleted

                if abs_path not in file_latest_ts or current_ts > file_latest_ts[abs_path]:
                    file_latest_ts[abs_path] = current_ts
                if abs_path not in file_first_ts or current_ts < file_first_ts[abs_path]:
                    file_first_ts[abs_path] = current_ts

    file_renames: dict[str, int] = defaultdict(int)
    try:
        rename_cmd = ["git", "log", "--name-status", "--oneline"]
        proc_rename = subprocess.Popen(
            rename_cmd,
            cwd=dirpath,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        rename_out, _ = proc_rename.communicate()
        if proc_rename.returncode == 0 and rename_out:
            for line in rename_out.splitlines():
                line = line.strip()
                if line.startswith("R") and "\t" in line:
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        old_p = os.path.abspath(os.path.join(dirpath, parts[1]))
                        new_p = os.path.abspath(os.path.join(dirpath, parts[2]))
                        file_renames[old_p] += 1
                        file_renames[new_p] += 1
    except Exception as e:
        logger.debug("Failed to count renames: %s", e)

    results = []
    for filepath, commit_count in file_commit_counts.items():
        latest_ts = file_latest_ts.get(filepath, int(now_ts))
        first_ts = file_first_ts.get(filepath, int(now_ts))

        days_since_change = max(0, int((now_ts - latest_ts) / 86400))
        survival_days = max(0, int((now_ts - first_ts) / 86400))

        author_map = file_author_counts.get(filepath, {})
        num_authors = len(author_map)
        total_author_commits = sum(author_map.values())
        entropy = 0.0
        if total_author_commits > 0:
            for cnt in author_map.values():
                p = cnt / total_author_commits
                if p > 0:
                    entropy -= p * math.log2(p)

        bug_fix_count = file_bug_fix_commits.get(filepath, 0)
        bug_fix_ratio = bug_fix_count / commit_count if commit_count > 0 else 0.0

        results.append({
            "filepath": filepath,
            "git_survival_days": survival_days,
            "git_rename_count": file_renames.get(filepath, 0),
            "git_ownership_entropy": round(entropy, 4),
            "git_commit_count": commit_count,
            "git_days_since_change": days_since_change,
            "git_churn": file_churn.get(filepath, 0),
            "git_authors": num_authors,
            "git_bug_fix_ratio": round(bug_fix_ratio, 4),
        })

    return results


def compute_git_coupling(dirpath: str) -> list:
    """Run git log to calculate file co-change coupling scores (Jaccard similarity).

    Returns list of (filepath_a, filepath_b, weight) tuples.
    """
    try:
        cmd = ["git", "log", "--pretty=format:commit:%H", "--name-only", "--max-count=10000", "--since=1.years"]
        proc = subprocess.Popen(
            cmd,
            cwd=dirpath,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        stdout, _ = proc.communicate()
        if proc.returncode != 0 or not stdout:
            return []
    except Exception as e:
        logger.debug("Failed to run git log: %s", e)
        return []

    commit_files = defaultdict(set)
    current_commit = None

    file_commit_counts = defaultdict(int)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("commit:"):
            current_commit = line.split(":", 1)[1]
        elif current_commit:
            abs_path = os.path.abspath(os.path.join(dirpath, line))
            if os.path.exists(abs_path):
                commit_files[current_commit].add(abs_path)

    pair_counts = defaultdict(lambda: defaultdict(int))
    for _commit, files in commit_files.items():
        if len(files) > 50 or len(files) < 2:
            continue
        for f in files:
            file_commit_counts[f] += 1

        file_list = list(files)
        for i in range(len(file_list)):
            for j in range(i + 1, len(file_list)):
                f1, f2 = file_list[i], file_list[j]
                if f1 < f2:
                    pair_counts[f1][f2] += 1
                else:
                    pair_counts[f2][f1] += 1

    coupling_edges = []
    for f1, targets in pair_counts.items():
        for f2, count in targets.items():
            denom = file_commit_counts[f1] + file_commit_counts[f2] - count
            if denom > 0:
                jaccard = count / denom
                if jaccard >= 0.1 and count >= 2:
                    coupling_edges.append((f1, f2, float(jaccard)))

    return coupling_edges
