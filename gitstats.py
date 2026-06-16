#!/usr/bin/env python3
"""
Gitstats3 - Git repository statistics generator.

This is the main entry point. All functionality is in the src/ package.
"""

import sys

# Version check
if sys.version_info < (3, 6):
    print("Python 3.6 or higher is required for gitstats", file=sys.stderr)
    sys.exit(1)

# Import everything from the src package for backward compatibility
from src import (
    # Configuration
    conf, get_config, GitStatsConfig,
    
    # Utilities
    ON_LINUX, WEEKDAYS,
    getkeyssortedbyvalues, getkeyssortedbyvaluekey,
    getstatsummarycounts, should_include_file,
    
    # Git commands
    getpipeoutput, getpipeoutput_list,
    get_default_branch, get_first_parent_flag,
    getlogrange, getcommitrange,
    is_git_repository, getversion, getgitversion,
    get_exectime_external,
    # OOP Metrics
    OOPMetricsAnalyzer, format_oop_report,
    
    # Core classes
    DataCollector,
    GitDataCollector,
    
    # Repository discovery
    discover_repositories,
    _discover_repositories_concurrent,
    _is_bare_repository,
    
    # CLI
    usage, GitStats,
)

# Re-export for backward compatibility
from src.gitstats_helpers import (
    get_output_format,
    exectime_internal,
    time_start,
)
from src.gitstats_analyzers import (
    getnumoffilesfromrev,
    getnumoflinesinblob,
    analyzesloc,
)


if __name__ == '__main__':
    try:
        g = GitStats()
        g.run(sys.argv[1:])
    except KeyboardInterrupt:
        print('\nInterrupted by user')
        sys.exit(1)
    except KeyError as e:
        print(f'FATAL: Configuration error: {e}')
        sys.exit(1)
    except Exception as e:
        print(f'FATAL: Unexpected error: {e}')
        if conf.get('debug', False):
            import traceback
            traceback.print_exc()
        sys.exit(1)