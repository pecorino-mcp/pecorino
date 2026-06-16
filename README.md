# gitstats3

A compact, single-file Git history statistics generator with an integrated
OOP-metrics analyzer.

This repository contains:

- `gitstats.py` — the main analysis script. It collects Git history and
  generates HTML reports (tables) and data files. The script now focuses on
  table-based output for accessibility and portability.
- `oop_metrics.py` — an analyzer that computes object-oriented design metrics
  (abstractness, instability, and Distance-from-Main-Sequence) for supported
  languages.
- `gitstats.css` — default stylesheet used by generated HTML reports.
- `scripts/get_repo_stats.py` — a small helper script to quickly report total
  commits and contributor counts for a repository or a tree of repositories.
- `requirements.txt` — Python package requirements used by parts of the
  project (see below).

## Quick summary

- Language: Python 3 (3.6+ runtime guarded in `gitstats.py`)
- Purpose: Produce repository statistics and design metrics from Git history
  and produce human-readable HTML tables with per-file and per-project metrics.
- Scope: Single-repo analysis and optional multi-repo scanning.

## Requirements

- Python 3.6 or newer (the code checks for 3.6+ at runtime).
- `git` available on PATH (used heavily by `gitstats.py` and `scripts/get_repo_stats.py`).

Minimal Python packages referenced in the repository are listed in
`requirements.txt`. At the time of writing it contains:

- `psutil==7.0.0`

Note: The main `gitstats.py` implementation uses command-line `git` and produces
HTML tables. It no longer depends on heavy plotting libraries by default.

If you want to install the Python requirements into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage - Basic

Analyze a single repository and write the report to an output folder:

```bash
python gitstats.py /path/to/git/repo /path/to/output
```

This performs repository history collection and writes an `index.html` plus
supporting files under the specified output folder. The output is table-based
HTML using `gitstats.css` for styling.

Common options (see `gitstats.py` for the complete CLI):

- `--verbose` — show progress and executed commands
- `--debug` — show debug traces (implies `--verbose`)
- `-c key=value` — override a configuration value from the internal `conf` dict
- `--multi-repo` — scan a directory tree for git repositories and produce
  per-repository reports (configurable via `-c` options)

Example (increase worker concurrency):

```bash
python gitstats.py -c processes=4 /path/to/repo /path/to/output
```

## Usage - Quick multi-repo scan

Scan a folder for Git repositories (creates separate reports under the output
folder):

```bash
python gitstats.py --multi-repo /path/to/repos /path/to/output
```

Tunable `conf` options (pass with `-c key=value`):

- `multi_repo_max_depth` — maximum directory depth to scan
- `multi_repo_include_patterns` — glob patterns to include
- `multi_repo_exclude_patterns` — glob patterns to exclude
- `multi_repo_parallel` — enable experimental parallel processing
- `multi_repo_max_workers` — number of worker processes if parallel is enabled

## Helper script: `scripts/get_repo_stats.py`

This small script quickly reports total commits and contributor counts.
It is useful for summaries and automation. Example:

```bash
python scripts/get_repo_stats.py --path /path/to/repo
python scripts/get_repo_stats.py --path /path/to/repos --recursive --json
```

The script uses `git rev-list --all --count` and `git shortlog -sne` for fast
counts and includes a safe fallback to counting unique author emails when
`shortlog` is not available.

## OOP metrics (in `oop_metrics.py`)

`oop_metrics.py` implements a language-aware analyzer that computes:

- Classes defined, abstract classes, interfaces
- Efferent coupling (Ce) and afferent coupling (Ca)
- Instability I = Ce / (Ce + Ca)
- Abstractness A = abstract classes / total classes
- Distance from Main Sequence D = |A + I - 1|

Supported/heuristic languages include: Python, Java, Scala, Kotlin, C/C++,
JavaScript/TypeScript, Swift, Go, Rust. The analyzer also attempts to build a
dependency graph (based on imports/includes) to estimate afferent coupling.

When `gitstats.py` runs, it integrates `OOPMetricsAnalyzer` and includes per-file
OOP metrics and package-level summaries in the final report. See
`format_oop_report()` in `oop_metrics.py` for the human-readable text format
used for console-style reporting.

Interpreting D (Distance):

- D < 0.2 — near the Main Sequence (balanced)
- 0.2 <= D <= 0.4 — moderate; refactoring may help
- D > 0.4 — poor; design issues likely (zone of pain or zone of uselessness)

## Configuration

`gitstats.py` contains an internal `conf` dictionary with sensible defaults.
You can override single settings from the command line with `-c key=value`.
Examples of configurable values: `processes`, `filter_by_extensions`,
`allowed_extensions`, `start_date`, `scan_default_branch_only`, and many
`multi_repo_*` settings.

## Development

- The code is intentionally kept in a compact layout for easy distribution and
  experimentation.
- The OOP metrics logic is located in `oop_metrics.py` and can be exercised
  independently (see `format_oop_report` and `OOPMetricsAnalyzer.analyze_file`).
- The main script uses `git` subprocess calls heavily; you can run it on any
  local repository where `git` is available.

Quick local development workflow:

```bash
# optional: create and activate a venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the tool locally (replace paths)
python gitstats.py /path/to/your/repo /tmp/gitstats-output
```

If you change `oop_metrics.py` and want to run a quick console report for a
single file, you can run a tiny snippet (Python REPL or script) that imports
`OOPMetricsAnalyzer`, calls `analyze_file()`, and prints `format_oop_report()`.

## Troubleshooting & notes

- Ensure `git` is installed and reachable in PATH.
- The project reads the Git history using shelling out to the `git` binary.
  If commands fail due to repository state or permissions, run with
  `--verbose`/`--debug` to see the invoked commands.
- The script filters files by extension by default; set `-c filter_by_extensions=false`
  to include all files in counts.
- For very large repositories or multi-repo scanning, tune `processes` and
  `multi_repo_max_workers` to a value appropriate for your machine.

## License

This repository does not contain an explicit license file. If you plan to use
or redistribute this code, add a suitable license (e.g. MIT, Apache-2.0) and
update this README accordingly.

## Contact / Contributing

Contributions, bug reports, and improvements are welcome. Open an issue or
submit a pull request with small, focused changes. For large refactors,
please open an issue first to discuss the design.


-- End of README

