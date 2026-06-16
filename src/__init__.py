"""
Gitstats3 - Modular Git Repository Statistics Package

This package provides comprehensive git repository analysis and HTML report generation.
All functionality is organized into focused modules for better maintainability.
"""

# Configuration
# Parsers
from .gitstats_ast import (
    ASTNode,
    AttributeDef,
    ClassDef,
    FunctionDef,
    ImportDef,
    InterfaceDef,
    ModuleDef,
    iter_child_nodes,
    walk,
)

# CLI
from .gitstats_cli import GitStats, usage
from .gitstats_config import (
    GitStatsConfig,
    conf,
    get_config,
    set_config,
    update_conf_from_config,
)
from .gitstats_constants import (
    ALLOWED_EXTENSIONS,
    CONTROL_FLOW_KEYWORDS,
    EXTENSION_TO_LANGUAGE,
    LANGUAGE_KEYWORDS,
    get_keywords_for_language,
    get_language_for_extension,
)

# Collectors
from .gitstats_datacollector import DataCollector

# Export
from .gitstats_export import MetricsExporter, export_to_json, export_to_yaml

# Git commands
from .gitstats_gitcommands import (
    get_default_branch,
    get_exectime_external,
    get_first_parent_flag,
    getcommitrange,
    getgitversion,
    getlogrange,
    getpipeoutput,
    getpipeoutput_list,
    getversion,
    is_git_repository,
    reset_exectime_external,
)
from .gitstats_gitdatacollector import GitDataCollector

# Utilities
from .gitstats_helpers import (
    ON_LINUX,
    WEEKDAYS,
    format_duration,
    getkeyssortedbyvaluekey,
    getkeyssortedbyvalues,
    getstatsummarycounts,
    parse_timestamp,
    sanitize_filename,
    should_include_file,
)

# Hotspot Detection
from .gitstats_hotspot import HotspotDetector, analyze_hotspots

# Metrics
from .gitstats_maintainability import (
    calculate_halstead_metrics,
    calculate_loc_metrics,
    calculate_maintainability_index,
    calculate_mccabe_complexity,
    interpret_maintainability_index,
)
from .gitstats_oopmetrics import OOPMetricsAnalyzer, format_oop_report

# Repository discovery
from .gitstats_repository import (
    _discover_repositories_concurrent,
    _is_bare_repository,
    discover_repositories,
)

# Tree-sitter Grammar Manager
from .tsgm import TreeSitterGrammarManager

__all__ = [
    # Config
    "GitStatsConfig",
    "get_config",
    "set_config",
    "conf",
    "update_conf_from_config",

    # Helpers
    "WEEKDAYS",
    "ON_LINUX",
    "getkeyssortedbyvalues",
    "getkeyssortedbyvaluekey",
    "getstatsummarycounts",
    "should_include_file",
    "format_duration",
    "sanitize_filename",
    "parse_timestamp",

    # Git commands
    "getpipeoutput",
    "getpipeoutput_list",
    "get_default_branch",
    "get_first_parent_flag",
    "getlogrange",
    "getcommitrange",
    "is_git_repository",
    "getversion",
    "getgitversion",
    "get_exectime_external",
    "reset_exectime_external",

    # Collectors
    "DataCollector",
    "GitDataCollector",

    # Parsers
    "ASTNode",
    "ModuleDef",
    "ClassDef",
    "InterfaceDef",
    "FunctionDef",
    "ImportDef",
    "AttributeDef",
    "walk",
    "iter_child_nodes",


    # Constants
    "ALLOWED_EXTENSIONS",
    "LANGUAGE_KEYWORDS",
    "EXTENSION_TO_LANGUAGE",
    "CONTROL_FLOW_KEYWORDS",
    "get_language_for_extension",
    "get_keywords_for_language",

    # Metrics
    "calculate_maintainability_index",
    "calculate_loc_metrics",
    "calculate_halstead_metrics",
    "calculate_mccabe_complexity",
    "interpret_maintainability_index",
    "OOPMetricsAnalyzer",
    "format_oop_report",

    # Repository discovery
    "_is_bare_repository",
    "discover_repositories",
    "_discover_repositories_concurrent",

    # CLI
    "usage",
    "GitStats",

    # Hotspot Detection
    "HotspotDetector",
    "analyze_hotspots",

    # Export
    "MetricsExporter",
    "export_to_json",
    "export_to_yaml",

    # Tree-sitter
    "TreeSitterGrammarManager",
]
