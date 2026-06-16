"""
Configuration module for Gitstats3.

Provides a dataclass-based configuration with sensible defaults
and a global configuration accessor.
"""

from dataclasses import dataclass, field
from typing import Set, Optional
import os

from .gitstats_constants import ALLOWED_EXTENSIONS


@dataclass
class GitStatsConfig:
    """Configuration settings for gitstats3 analysis."""
    
    # Display settings
    max_domains: int = 10
    max_ext_length: int = 10
    style: str = 'gitstats.css'
    max_authors: int = 20
    authors_top: int = 5
    
    # Commit range settings
    commit_begin: str = ''
    commit_end: str = 'HEAD'
    linear_linestats: int = 1
    project_name: str = ''
    start_date: str = ''
    
    # Processing settings (optimized for low-resource systems)
    processes: int = field(default_factory=lambda: min(2, os.cpu_count() or 1))
    
    # Low memory mode settings
    low_memory_mode: bool = False
    max_cache_entries: int = 1000  # Limit cache size to reduce memory usage
    chunk_size: int = 500  # Process commits in chunks to reduce peak memory
    gc_threshold_mb: int = 512  # Run garbage collection when exceeding this
    
    # Debug settings
    debug: bool = False
    verbose: bool = False
    
    # Branch scanning
    scan_default_branch_only: bool = True
    
    # Multi-repo configuration
    multi_repo_max_depth: int = 10
    multi_repo_include_patterns: Optional[str] = None
    multi_repo_exclude_patterns: Optional[str] = None
    multi_repo_parallel: bool = False
    multi_repo_max_workers: int = 2
    multi_repo_timeout: int = 3600  # 1 hour
    multi_repo_cleanup_on_error: bool = True
    multi_repo_fast_scan: bool = True
    multi_repo_batch_size: int = 10
    multi_repo_progress_interval: int = 5
    
    # File extension filtering (uses centralized constants by default)
    allowed_extensions: Set[str] = field(default_factory=lambda: ALLOWED_EXTENSIONS.copy())
    filter_by_extensions: bool = True
    calculate_mi_per_repository: bool = True
    
    def to_dict(self) -> dict:
        """Convert config to dictionary (for backward compatibility)."""
        return {
            'max_domains': self.max_domains,
            'max_ext_length': self.max_ext_length,
            'style': self.style,
            'max_authors': self.max_authors,
            'authors_top': self.authors_top,
            'commit_begin': self.commit_begin,
            'commit_end': self.commit_end,
            'linear_linestats': self.linear_linestats,
            'project_name': self.project_name,
            'start_date': self.start_date,
            'processes': self.processes,
            'debug': self.debug,
            'verbose': self.verbose,
            'scan_default_branch_only': self.scan_default_branch_only,
            'multi_repo_max_depth': self.multi_repo_max_depth,
            'multi_repo_include_patterns': self.multi_repo_include_patterns,
            'multi_repo_exclude_patterns': self.multi_repo_exclude_patterns,
            'multi_repo_parallel': self.multi_repo_parallel,
            'multi_repo_max_workers': self.multi_repo_max_workers,
            'multi_repo_timeout': self.multi_repo_timeout,
            'multi_repo_cleanup_on_error': self.multi_repo_cleanup_on_error,
            'multi_repo_fast_scan': self.multi_repo_fast_scan,
            'multi_repo_batch_size': self.multi_repo_batch_size,
            'multi_repo_progress_interval': self.multi_repo_progress_interval,
            'allowed_extensions': self.allowed_extensions,
            'filter_by_extensions': self.filter_by_extensions,
            'calculate_mi_per_repository': self.calculate_mi_per_repository,
            'low_memory_mode': self.low_memory_mode,
            'max_cache_entries': self.max_cache_entries,
            'chunk_size': self.chunk_size,
            'gc_threshold_mb': self.gc_threshold_mb,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> 'GitStatsConfig':
        """Create config from dictionary."""
        config = cls()
        for key, value in d.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config


# Global configuration instance
_config: GitStatsConfig = GitStatsConfig()


def get_config() -> GitStatsConfig:
    """Get the global configuration instance."""
    return _config


def set_config(config: GitStatsConfig) -> None:
    """Set the global configuration instance."""
    global _config
    _config = config


# Backward compatibility: dict-like access
conf = _config.to_dict()


def update_conf_from_config():
    """Update the backward-compatible conf dict from current config."""
    global conf
    conf = _config.to_dict()
