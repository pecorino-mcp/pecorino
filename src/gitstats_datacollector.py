"""
Data collector module for Gitstats3.

Contains the base DataCollector class with repository metrics collection.
"""

import datetime
import os
import pickle
import re
import time
import zlib
from collections import OrderedDict, defaultdict

from .gitstats_config import conf
from .gitstats_gitcommands import getpipeoutput
from .gitstats_helpers import should_include_file
from .gitstats_oopmetrics import OOPMetricsAnalyzer


class LRUCache(OrderedDict):
    """Size-limited LRU cache to prevent unbounded memory growth."""

    def __init__(self, maxsize=1000):
        super().__init__()
        self.maxsize = maxsize

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)

    def __contains__(self, key):
        return OrderedDict.__contains__(self, key)


class DataCollector:
	"""Consolidated data collector for repository metrics with optimized memory usage."""
	def __init__(self):
		self.stamp_created = time.time()
		# Use LRU cache with bounded size for low-memory systems
		self.cache = LRUCache(maxsize=conf.get('max_cache_entries', 1000))
		self.total_authors = 0

		# Initialize OOP Metrics Analyzer for Distance from Main Sequence analysis
		self.oop_analyzer = OOPMetricsAnalyzer()

		# Core activity metrics - consolidated for memory efficiency
		self.activity_metrics = {
			'by_hour_of_day': defaultdict(int),
			'by_day_of_week': defaultdict(int),
			'by_month_of_year': defaultdict(int),
			'by_hour_of_week': defaultdict(lambda: defaultdict(int)),
			'by_year_week': defaultdict(int),
			'hour_of_day_busiest': 0,
			'hour_of_week_busiest': 0,
			'year_week_peak': 0
		}

		# Core repository statistics - consolidated
		self.repository_stats = {
			'total_commits': 0,
			'total_files': 0,
			'total_lines': 0,
			'total_lines_added': 0,
			'total_lines_removed': 0,
			'total_size': 0,
			'first_commit_stamp': 0,
			'last_commit_stamp': 0,
			'last_active_day': None,
			'active_days': set(),
			'repository_size_mb': 0.0
		}

		# Author data - consolidated structure
		self.authors = {}  # name -> all author metrics
		self.authors_by_commits = 0

		# Domain and timezone analysis
		self.domains = defaultdict(lambda: defaultdict(int))
		self.commits_by_timezone = defaultdict(int)

		# Temporal analysis - consolidated
		self.temporal_data = {
			'author_of_month': defaultdict(lambda: defaultdict(int)),
			'author_of_year': defaultdict(lambda: defaultdict(int)),
			'commits_by_month': defaultdict(int),
			'commits_by_year': defaultdict(int),
			'lines_added_by_month': defaultdict(int),
			'lines_added_by_year': defaultdict(int),
			'lines_removed_by_month': defaultdict(int),
			'lines_removed_by_year': defaultdict(int),
			'lines_added_by_author_by_year': defaultdict(lambda: defaultdict(int)),
			'commits_by_author_by_year': defaultdict(lambda: defaultdict(int)),
			'files_by_year': defaultdict(int),
			'files_by_stamp': {},
			'changes_by_date': {},
			'pace_of_changes': {},
			'pace_of_changes_by_month': defaultdict(int),
			'pace_of_changes_by_year': defaultdict(int)
		}

		# Recent activity tracking - consolidated
		self.recent_activity = {
			'last_30_days_commits': 0,
			'last_30_days_lines_added': 0,
			'last_30_days_lines_removed': 0,
			'last_12_months_commits': defaultdict(int),
			'last_12_months_lines_added': defaultdict(int),
			'last_12_months_lines_removed': defaultdict(int)
		}

		# Code quality and structure metrics - consolidated
		self.code_analysis = {
			# SLOC analysis
			'total_source_lines': 0,
			'total_comment_lines': 0,
			'total_blank_lines': 0,
			'sloc_by_extension': {},

			# File analysis
			'extensions': {},
			'file_sizes': {},
			'file_revisions': {},
			'file_types': defaultdict(int),
			'large_files': [],
			'complex_files': [],
			'hot_files': [],

			# Directory analysis
			'directories': defaultdict(lambda: {
				'commits': 0, 'lines_added': 0, 'lines_removed': 0, 'files': set()
			}),
			'directory_revisions': defaultdict(int)
		}

		# Enhanced project health metrics - consolidated and optimized
		self.project_health = {


			# Code quality metrics
			'code_quality': {
				'cyclomatic_complexity': 0,
				'maintainability_index': 0.0,
				'technical_debt_minutes': 0
			},

			# Team collaboration metrics
			'collaboration': {
				'bus_factor': 0,
				'knowledge_concentration': {},
				'cross_team_commits': 0
			}
		}

		# Branch analysis - consolidated
		self.branch_analysis = {
			'branches': {},
			'unmerged_branches': [],
			'main_branch': 'master'
		}

		# Team analysis - consolidated for memory efficiency
		self.team_analysis = {
			'author_collaboration': {},
			'commit_patterns': {},
			'working_patterns': {},
			'impact_analysis': {},
			'team_performance': {},
			'critical_files': set(),
			'file_impact_scores': {},
			'author_active_periods': {}
		}

		# Commit categorization
		self.commit_categories = {
			'bug_commits': [],
			'refactoring_commits': [],
			'feature_commits': []
		}

		# Tags
		self.tags = {}

		# Track changes by date by author
		self.changes_by_date_by_author = {}

		# Compatibility properties for backward compatibility
		self._setup_compatibility_properties()

	def _setup_compatibility_properties(self):
		"""Setup properties for backward compatibility with existing code."""
		# Map old property names to new consolidated structures
		pass  # Properties will be handled by @property decorators

	def get_consolidated_metrics(self):
		"""Get a comprehensive summary of all metrics in consolidated format."""
		return {
			'repository_stats': self.repository_stats,
			'activity_metrics': dict(self.activity_metrics),  # Convert defaultdicts to regular dicts
			'temporal_data': {k: (dict(v) if hasattr(v, 'items') and callable(getattr(v, 'items', None)) else v)
							 for k, v in self.temporal_data.items()},
			'recent_activity': dict(self.recent_activity),
			'code_analysis': {k: (dict(v) if hasattr(v, 'items') and callable(getattr(v, 'items', None)) else v)
							 for k, v in self.code_analysis.items()},
			'project_health': self.project_health,
			'branch_analysis': self.branch_analysis,
			'team_analysis': self.team_analysis,
			'commit_categories': self.commit_categories,
			'authors': self.authors,
			'tags': self.tags,
			'domains': {k: dict(v) for k, v in self.domains.items()}
		}

	def update_memory_efficient_metrics(self, metric_type, key, value=None, increment=1):
		"""Memory-efficient metric updating with consolidated access patterns."""
		metric_maps = {
			'activity': self.activity_metrics,
			'repository': self.repository_stats,
			'temporal': self.temporal_data,
			'recent': self.recent_activity,
			'code': self.code_analysis,
			'health': self.project_health,
			'branch': self.branch_analysis,
			'team': self.team_analysis
		}

		if metric_type in metric_maps:
			if value is not None:
				metric_maps[metric_type][key] = value
			else:
				metric_maps[metric_type][key] += increment
			return True
		return False

	def optimize_memory_usage(self):
		"""Optimize memory usage by converting defaultdicts to regular dicts and cleaning up cache."""
		# Convert defaultdicts to regular dicts to save memory
		for metric_group in [self.activity_metrics, self.temporal_data, self.recent_activity]:
			for key, value in metric_group.items():
				if hasattr(value, 'default_factory') and value.default_factory:
					metric_group[key] = dict(value)

		# Convert nested defaultdicts
		for key, value in self.temporal_data.items():
			if hasattr(value, 'items'):
				for subkey, subvalue in value.items():
					if hasattr(subvalue, 'default_factory') and subvalue.default_factory:
						value[subkey] = dict(subvalue)

		# Clean up large cache items that are no longer needed
		if 'files_in_tree' in self.cache and len(self.cache['files_in_tree']) > 10000:
			# Keep only most recent 5000 entries
			items = list(self.cache['files_in_tree'].items())
			self.cache['files_in_tree'] = dict(items[-5000:])

		if 'lines_in_blob' in self.cache and len(self.cache['lines_in_blob']) > 10000:
			# Keep only most recent 5000 entries
			items = list(self.cache['lines_in_blob'].items())
			self.cache['lines_in_blob'] = dict(items[-5000:])

		return True

	# Backward compatibility properties
	@property
	def activity_by_hour_of_day(self):
		return self.activity_metrics['by_hour_of_day']

	@property
	def activity_by_day_of_week(self):
		return self.activity_metrics['by_day_of_week']

	@property
	def activity_by_month_of_year(self):
		return self.activity_metrics['by_month_of_year']

	@property
	def activity_by_hour_of_week(self):
		return self.activity_metrics['by_hour_of_week']

	@property
	def activity_by_year_week(self):
		return self.activity_metrics['by_year_week']

	@property
	def activity_by_hour_of_day_busiest(self):
		return self.activity_metrics['hour_of_day_busiest']

	@activity_by_hour_of_day_busiest.setter
	def activity_by_hour_of_day_busiest(self, value):
		self.activity_metrics['hour_of_day_busiest'] = value

	@property
	def activity_by_hour_of_week_busiest(self):
		return self.activity_metrics['hour_of_week_busiest']

	@activity_by_hour_of_week_busiest.setter
	def activity_by_hour_of_week_busiest(self, value):
		self.activity_metrics['hour_of_week_busiest'] = value

	@property
	def activity_by_year_week_peak(self):
		return self.activity_metrics['year_week_peak']

	@activity_by_year_week_peak.setter
	def activity_by_year_week_peak(self, value):
		self.activity_metrics['year_week_peak'] = value

	# Repository stats properties
	@property
	def total_commits(self):
		return self.repository_stats['total_commits']

	@total_commits.setter
	def total_commits(self, value):
		self.repository_stats['total_commits'] = value

	@property
	def total_files(self):
		return self.repository_stats['total_files']

	@total_files.setter
	def total_files(self, value):
		self.repository_stats['total_files'] = value

	@property
	def total_lines(self):
		return self.repository_stats['total_lines']

	@total_lines.setter
	def total_lines(self, value):
		self.repository_stats['total_lines'] = value

	@property
	def total_lines_added(self):
		return self.repository_stats['total_lines_added']

	@total_lines_added.setter
	def total_lines_added(self, value):
		self.repository_stats['total_lines_added'] = value

	@property
	def total_lines_removed(self):
		return self.repository_stats['total_lines_removed']

	@total_lines_removed.setter
	def total_lines_removed(self, value):
		self.repository_stats['total_lines_removed'] = value

	@property
	def total_size(self):
		return self.repository_stats['total_size']

	@total_size.setter
	def total_size(self, value):
		self.repository_stats['total_size'] = value

	@property
	def first_commit_stamp(self):
		return self.repository_stats['first_commit_stamp']

	@first_commit_stamp.setter
	def first_commit_stamp(self, value):
		self.repository_stats['first_commit_stamp'] = value

	@property
	def last_commit_stamp(self):
		return self.repository_stats['last_commit_stamp']

	@last_commit_stamp.setter
	def last_commit_stamp(self, value):
		self.repository_stats['last_commit_stamp'] = value

	@property
	def last_active_day(self):
		return self.repository_stats['last_active_day']

	@last_active_day.setter
	def last_active_day(self, value):
		self.repository_stats['last_active_day'] = value

	@property
	def active_days(self):
		return self.repository_stats['active_days']

	@property
	def repository_size_mb(self):
		return self.repository_stats['repository_size_mb']

	@repository_size_mb.setter
	def repository_size_mb(self, value):
		self.repository_stats['repository_size_mb'] = value

	# Temporal data properties
	@property
	def author_of_month(self):
		return self.temporal_data['author_of_month']

	@property
	def author_of_year(self):
		return self.temporal_data['author_of_year']

	@property
	def commits_by_month(self):
		return self.temporal_data['commits_by_month']

	@property
	def commits_by_year(self):
		return self.temporal_data['commits_by_year']

	@property
	def lines_added_by_month(self):
		return self.temporal_data['lines_added_by_month']

	@property
	def lines_added_by_year(self):
		return self.temporal_data['lines_added_by_year']

	@property
	def lines_removed_by_month(self):
		return self.temporal_data['lines_removed_by_month']

	@property
	def lines_removed_by_year(self):
		return self.temporal_data['lines_removed_by_year']

	@property
	def lines_added_by_author_by_year(self):
		return self.temporal_data['lines_added_by_author_by_year']

	@property
	def commits_by_author_by_year(self):
		return self.temporal_data['commits_by_author_by_year']

	@property
	def files_by_year(self):
		return self.temporal_data['files_by_year']

	@property
	def files_by_stamp(self):
		return self.temporal_data['files_by_stamp']

	@property
	def changes_by_date(self):
		return self.temporal_data['changes_by_date']

	@property
	def pace_of_changes(self):
		return self.temporal_data['pace_of_changes']

	@property
	def pace_of_changes_by_month(self):
		return self.temporal_data['pace_of_changes_by_month']

	@property
	def pace_of_changes_by_year(self):
		return self.temporal_data['pace_of_changes_by_year']

	# Recent activity properties
	@property
	def last_30_days_commits(self):
		return self.recent_activity['last_30_days_commits']

	@last_30_days_commits.setter
	def last_30_days_commits(self, value):
		self.recent_activity['last_30_days_commits'] = value

	@property
	def last_30_days_lines_added(self):
		return self.recent_activity['last_30_days_lines_added']

	@last_30_days_lines_added.setter
	def last_30_days_lines_added(self, value):
		self.recent_activity['last_30_days_lines_added'] = value

	@property
	def last_30_days_lines_removed(self):
		return self.recent_activity['last_30_days_lines_removed']

	@last_30_days_lines_removed.setter
	def last_30_days_lines_removed(self, value):
		self.recent_activity['last_30_days_lines_removed'] = value

	@property
	def last_12_months_commits(self):
		return self.recent_activity['last_12_months_commits']

	@property
	def last_12_months_lines_added(self):
		return self.recent_activity['last_12_months_lines_added']

	@property
	def last_12_months_lines_removed(self):
		return self.recent_activity['last_12_months_lines_removed']

	# Code analysis properties
	@property
	def total_source_lines(self):
		return self.code_analysis['total_source_lines']

	@total_source_lines.setter
	def total_source_lines(self, value):
		self.code_analysis['total_source_lines'] = value

	@property
	def total_comment_lines(self):
		return self.code_analysis['total_comment_lines']

	@total_comment_lines.setter
	def total_comment_lines(self, value):
		self.code_analysis['total_comment_lines'] = value

	@property
	def total_blank_lines(self):
		return self.code_analysis['total_blank_lines']

	@total_blank_lines.setter
	def total_blank_lines(self, value):
		self.code_analysis['total_blank_lines'] = value

	@property
	def sloc_by_extension(self):
		return self.code_analysis['sloc_by_extension']

	@property
	def extensions(self):
		return self.code_analysis['extensions']

	@property
	def file_sizes(self):
		return self.code_analysis['file_sizes']

	@property
	def file_revisions(self):
		return self.code_analysis['file_revisions']

	@property
	def directories(self):
		return self.code_analysis['directories']

	@property
	def directory_revisions(self):
		return self.code_analysis['directory_revisions']

	# Project health properties
	@property
	def documentation_metrics(self):
		return self.project_health['documentation_quality']

	@property
	def code_quality_metrics(self):
		return self.project_health['code_quality']

	@property
	def collaboration_metrics(self):
		return self.project_health['collaboration']

	@property
	def file_analysis(self):
		return {
			'file_types': self.code_analysis['file_types'],
			'large_files': self.code_analysis['large_files'],
			'complex_files': self.code_analysis['complex_files'],
			'hot_files': self.code_analysis['hot_files']
		}

	# Branch analysis properties
	@property
	def branches(self):
		return self.branch_analysis['branches']

	@branches.setter
	def branches(self, value):
		self.branch_analysis['branches'] = value

	@property
	def unmerged_branches(self):
		return self.branch_analysis['unmerged_branches']

	@unmerged_branches.setter
	def unmerged_branches(self, value):
		self.branch_analysis['unmerged_branches'] = value

	@property
	def main_branch(self):
		return self.branch_analysis['main_branch']

	@main_branch.setter
	def main_branch(self, value):
		self.branch_analysis['main_branch'] = value

	# Team analysis properties
	@property
	def author_collaboration(self):
		return self.team_analysis['author_collaboration']

	@property
	def commit_patterns(self):
		return self.team_analysis['commit_patterns']

	@property
	def working_patterns(self):
		return self.team_analysis['working_patterns']

	@property
	def impact_analysis(self):
		return self.team_analysis['impact_analysis']

	@property
	def team_performance(self):
		return self.team_analysis['team_performance']

	@property
	def critical_files(self):
		return self.team_analysis['critical_files']

	@property
	def file_impact_scores(self):
		return self.team_analysis['file_impact_scores']

	@property
	def author_active_periods(self):
		return self.team_analysis['author_active_periods']

	# Commit categorization properties
	@property
	def potential_bug_commits(self):
		return self.commit_categories['bug_commits']

	@property
	def refactoring_commits(self):
		return self.commit_categories['refactoring_commits']

	@property
	def feature_commits(self):
		return self.commit_categories['feature_commits']

	@property
	def comprehensive_metrics(self):
		return self.project_health.get('comprehensive_metrics', {})

	@comprehensive_metrics.setter
	def comprehensive_metrics(self, value):
		self.project_health['comprehensive_metrics'] = value

	@property
	def oop_metrics(self):
		if not hasattr(self, '_oop_metrics'):
			self._oop_metrics = {}
		return self._oop_metrics

	@oop_metrics.setter
	def oop_metrics(self, value):
		self._oop_metrics = value

	##
	# This should be the main function to extract data from the repository.
	def collect(self, dir):
		self.dir = dir
		# Re-initialize OOP Metrics Analyzer with the correct repository path
		self.oop_analyzer = OOPMetricsAnalyzer(repo_path=dir)
		if len(conf['project_name']) == 0:
			self.projectname = os.path.basename(os.path.abspath(dir))
		else:
			self.projectname = conf['project_name']

	##
	# Load cacheable data
	def loadCache(self, cachefile):
		if not os.path.exists(cachefile):
			return
		print('Loading cache...')
		try:
			with open(cachefile, 'rb') as f:
				try:
					self.cache = pickle.loads(zlib.decompress(f.read()))
				except (zlib.error, pickle.PickleError):
					# temporary hack to upgrade non-compressed caches
					try:
						f.seek(0)
						self.cache = pickle.load(f)
					except (pickle.PickleError, EOFError) as e2:
						print(f'Warning: Failed to load cache file {cachefile}: {e2}')
						self.cache = {}
				except Exception as e:
					print(f'Warning: Unexpected error loading cache file {cachefile}: {e}')
					self.cache = {}
		except OSError as e:
			print(f'Warning: Could not open cache file {cachefile}: {e}')
			self.cache = {}

	##
	# Produce any additional statistics from the extracted data.
	def refine(self):
		pass

	##
	# : get a dictionary of author
	def getAuthorInfo(self, author):
		return None

	def getActivityByDayOfWeek(self):
		return {}

	def getActivityByHourOfDay(self):
		return {}

	# : get a dictionary of domains
	def getDomainInfo(self, domain):
		return {}

	##
	# Get a list of authors
	def getAuthors(self):
		return []

	def getFirstCommitDate(self):
		return datetime.datetime.now()

	def getLastCommitDate(self):
		return datetime.datetime.now()

	def getStampCreated(self):
		return self.stamp_created

	# Enhanced Metrics Calculation Methods - College Project Implementation



	def calculate_bus_factor(self):
		"""Calculate knowledge distribution risk (Bus Factor)"""
		if not self.authors:
			return 0

		# Sort authors by commit count
		author_commits = [(author, data['commits']) for author, data in self.authors.items()]
		author_commits.sort(key=lambda x: x[1], reverse=True)

		total_commits = sum(commits for _, commits in author_commits)
		if total_commits == 0:
			return 0

		# Calculate cumulative percentage
		cumulative = 0
		bus_factor = 0

		for author, commits in author_commits:
			cumulative += commits
			bus_factor += 1
			# If top N authors have >50% of commits, that's the bus factor
			if (cumulative / total_commits) >= 0.5:
				break

		return bus_factor

	def calculate_code_quality_score(self):
		"""Calculate overall code quality using industry standards"""
		scores = []

		# Complexity score (lower is better)
		if self.repository_stats['total_files'] > 0:
			avg_complexity = self.project_health['code_quality']['cyclomatic_complexity'] / self.repository_stats['total_files']
			complexity_score = max(0, 100 - (avg_complexity - 10) * 10)  # Penalize complexity > 10
			scores.append(complexity_score)



		# Team collaboration score
		bus_factor = self.calculate_bus_factor()
		collaboration_score = min(bus_factor * 25, 100)  # Higher bus factor = better
		scores.append(collaboration_score)

		# File organization score
		if self.repository_stats['total_files'] > 0:
			large_files_ratio = len(self.code_analysis['large_files']) / self.repository_stats['total_files']
			file_org_score = max(0, 100 - (large_files_ratio * 100))
			scores.append(file_org_score)

		# Calculate weighted average
		if scores:
			return sum(scores) / len(scores)
		return 0.0

	def update_enhanced_metrics(self, filepath):
		"""Update enhanced metrics for a given file"""
		try:
			with open(filepath, encoding='utf-8', errors='ignore') as f:
				content = f.read()
				lines = content.split('\n')

			# File type analysis
			ext = os.path.splitext(filepath)[1].lower()
			self.code_analysis['file_types'][ext] += 1

			# Large files detection (>500 LOC)
			if len(lines) > 500:
				self.code_analysis['large_files'].append(filepath)

			# Documentation analysis
			comment_lines = 0
			for line in lines:
				line = line.strip()
				if line.startswith('#') or line.startswith('//') or line.startswith('*') or line.startswith('/*'):
					comment_lines += 1
				if '"""' in line or "'''" in line:
					comment_lines += 1

			# Complexity analysis - use proper McCabe calculation
			mccabe_result = self._calculate_mccabe_complexity(content, ext)
			complexity = mccabe_result.get('cyclomatic_complexity', 0)
			self.project_health['code_quality']['cyclomatic_complexity'] += complexity

			if complexity > 20:  # High complexity threshold
				self.code_analysis['complex_files'].append(filepath)

		except Exception:
			pass  # Skip files that can't be read



	def getTags(self):
		return []

	def getTotalAuthors(self):
		return -1

	def getTotalCommits(self):
		return -1

	def getTotalFiles(self):
		return -1

	def getTotalLOC(self):
		return -1

	##
	# Save cacheable data
	def saveCache(self, cachefile):
		print('Saving cache...')
		tempfile = cachefile + '.tmp'
		try:
			# Optimize cache before saving - remove old/stale entries
			optimized_cache = self._optimizeCache()

			with open(tempfile, 'wb') as f:
				# Use higher compression level for better storage efficiency
				data = zlib.compress(pickle.dumps(optimized_cache), level=6)
				f.write(data)
			try:
				os.remove(cachefile)
			except OSError:
				pass
			os.rename(tempfile, cachefile)

			if conf['verbose']:
				cache_size_mb = os.path.getsize(cachefile) / (1024 * 1024)
				print(f'Cache saved: {cache_size_mb:.2f} MB')

		except OSError as e:
			print(f'Warning: Could not save cache file {cachefile}: {e}')
			# Clean up temp file if it exists
			try:
				os.remove(tempfile)
			except OSError:
				pass

	def _optimizeCache(self):
		"""Optimize cache by removing old or unnecessary entries."""
		if not hasattr(self, 'cache') or not self.cache:
			return {}

		optimized = {}

		# Keep essential cache entries
		for key in ['files_in_tree', 'lines_in_blob']:
			if key in self.cache:
				cache_data = self.cache[key]

				# For files_in_tree, keep only recent entries (limit to prevent unbounded growth)
				if key == 'files_in_tree' and len(cache_data) > 10000:
					# Keep most recent 10000 entries
					sorted_items = sorted(cache_data.items())[-10000:]
					optimized[key] = dict(sorted_items)
					if conf['verbose']:
						print(f'Optimized {key} cache: {len(cache_data)} -> {len(optimized[key])} entries')
				else:
					optimized[key] = cache_data

		return optimized

	def calculate_comprehensive_metrics(self, filepath):
		"""Calculate comprehensive code metrics for a file including LOC, Halstead, and McCabe metrics."""
		try:
			with open(filepath, encoding='utf-8', errors='ignore') as f:
				content = f.read()

			# Get file extension for language-specific analysis
			ext = os.path.splitext(filepath)[1].lower()

			# Calculate all metrics
			loc_metrics = self._calculate_loc_metrics(content, ext)
			halstead_metrics = self._calculate_halstead_metrics(content, ext)
			mccabe_metrics = self._calculate_mccabe_complexity(content, ext)
			maintainability_index = self._calculate_maintainability_index(loc_metrics, halstead_metrics, mccabe_metrics)
			oop_metrics = self._calculate_oop_metrics(content, ext, filepath)

			# Calculate OOP metrics using the dedicated analyzer for Distance from Main Sequence
			oop_distance_metrics = self.oop_analyzer.analyze_file(filepath, content, ext)

			# Pre-calculate complexity_score for hotspot analysis (avoids redundant calculation)
			mi_value = maintainability_index.get('mi', 100)
			cyclomatic = mccabe_metrics.get('cyclomatic_complexity', 0)
			mi_part = max(0, 100 - (mi_value / 171 * 100))
			cc_part = min(cyclomatic * 2, 100)
			complexity_score = (mi_part * 0.6) + (cc_part * 0.4)

			return {
				'loc': loc_metrics,
				'halstead': halstead_metrics,
				'mccabe': mccabe_metrics,
				'maintainability_index': maintainability_index,
				'complexity_score': complexity_score,  # Cached for hotspot analysis
				'oop': oop_metrics,
				'oop_distance': oop_distance_metrics,  # New: Distance from Main Sequence metrics
				'filepath': filepath,
				'extension': ext
			}

		except Exception as e:
			if conf['debug']:
				print(f'Warning: Failed to calculate metrics for {filepath}: {e}')
			return None

	def _calculate_loc_metrics(self, content, file_extension):
		"""Calculate Lines-of-Code metrics (LOCphy, LOCbl, LOCpro, LOCcom)."""
		lines = content.split('\n')

		# Initialize counters
		loc_phy = len(lines)  # Physical lines
		loc_bl = 0   # Blank lines
		loc_pro = 0  # Program lines (declarations, definitions, directives, code)
		loc_com = 0  # Comment lines

		# Language-specific comment patterns
		comment_patterns = self._get_comment_patterns(file_extension)

		in_multiline_comment = False

		for line in lines:
			original_line = line
			line_stripped = line.strip()

			# Check if line is blank
			if not line_stripped:
				loc_bl += 1
				continue

			# Handle multi-line comments
			if file_extension in ['.c', '.cpp', '.java', '.js', '.ts', '.css', '.h', '.hpp']:
				if '/*' in line_stripped and '*/' in line_stripped:
					# Single line /* */ comment
					loc_com += 1
					continue
				elif '/*' in line_stripped:
					in_multiline_comment = True
					loc_com += 1
					continue
				elif '*/' in line_stripped:
					in_multiline_comment = False
					loc_com += 1
					continue
				elif in_multiline_comment:
					loc_com += 1
					continue

			# Check for single-line comments
			is_comment = False
			for pattern in comment_patterns:
				if re.match(pattern, line_stripped):
					loc_com += 1
					is_comment = True
					break

			# If not a comment or blank line, it's a program line
			if not is_comment:
				# Check for mixed lines (code + comment on same line)
				has_code = True

				# Simple heuristic: if line starts with comment, it's a comment
				# Otherwise, it's code (even if it has trailing comments)
				for pattern in comment_patterns:
					if re.match(pattern, line_stripped):
						has_code = False
						break

				if has_code:
					loc_pro += 1

		return {
			'loc_phy': loc_phy,
			'loc_bl': loc_bl,
			'loc_pro': loc_pro,
			'loc_com': loc_com,
			'comment_ratio': (loc_com / loc_phy * 100) if loc_phy > 0 else 0.0
		}

	def _get_comment_patterns(self, file_extension):
		"""Get regex patterns for comments based on file extension."""
		patterns = {
			'.py': [r'^\s*#'],
			'.js': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.ts': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.jsx': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.tsx': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.java': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.scala': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.kt': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.cpp': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.c': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.cc': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.cxx': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.h': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.hpp': [r'^\s*//', r'^\s*/\*', r'^\s*\*'],
			'.go': [r'^\s*//'],
			'.rs': [r'^\s*//', r'^\s*///'],
			'.php': [r'^\s*//', r'^\s*/\*', r'^\s*\*', r'^\s*#'],
			'.rb': [r'^\s*#'],
			'.pl': [r'^\s*#'],
			'.sh': [r'^\s*#'],
			'.css': [r'^\s*/\*', r'^\s*\*'],
			'.html': [r'^\s*<!--'],
			'.xml': [r'^\s*<!--'],
		}

		return patterns.get(file_extension, [r'^\s*#', r'^\s*//', r'^\s*/\*'])

	def _calculate_halstead_metrics(self, content, file_extension):
		"""Calculate Halstead complexity metrics."""

		# Language-specific operators and operands patterns
		operators, operand_patterns = self._get_halstead_patterns(file_extension)

		# Remove comments and strings to avoid false positives
		cleaned_content = self._remove_comments_and_strings(content, file_extension)

		# Count operators
		n1_dict = {}  # distinct operators
		N1 = 0        # total operators

		for op in operators:
			# Escape special regex characters
			escaped_op = re.escape(op)
			matches = re.findall(escaped_op, cleaned_content)
			if matches:
				count = len(matches)
				n1_dict[op] = count
				N1 += count

		# Count operands (identifiers, numbers, strings)
		n2_dict = {}  # distinct operands
		N2 = 0        # total operands

		# Find all potential operands
		for pattern in operand_patterns:
			matches = re.findall(pattern, cleaned_content)
			for match in matches:
				if isinstance(match, tuple):
					match = match[0] if match[0] else match[1] if len(match) > 1 else ''

				if match and match not in n1_dict:  # Don't count operators as operands
					if match in n2_dict:
						n2_dict[match] += 1
					else:
						n2_dict[match] = 1
					N2 += 1

		# Calculate base metrics
		n1 = len(n1_dict)  # number of distinct operators
		n2 = len(n2_dict)  # number of distinct operands

		# Calculate derived metrics
		import math

		N = N1 + N2  # Program length
		n = n1 + n2  # Vocabulary

		if n > 0 and N > 0:
			V = N * math.log2(n)  # Program volume
		else:
			V = 0

		if n2 > 0 and n1 > 0:
			D = (n1 / 2) * (N2 / n2)  # Difficulty
		else:
			D = 0

		L = 1 / D if D > 0 else 0  # Level
		E = V * D if V > 0 and D > 0 else 0  # Effort
		T = E / 18 if E > 0 else 0  # Time (seconds)

		if E > 0:
			B = (E ** (2/3)) / 3000  # Estimated delivered bugs
		else:
			B = 0

		return {
			'n1': n1,           # distinct operators
			'n2': n2,           # distinct operands
			'N1': N1,           # total operators
			'N2': N2,           # total operands
			'N': N,             # program length
			'n': n,             # vocabulary
			'V': V,             # program volume
			'D': D,             # difficulty
			'L': L,             # level
			'E': E,             # effort
			'T': T,             # time
			'B': B              # estimated bugs
		}

	def _get_halstead_patterns(self, file_extension):
		"""Get operators and operand patterns for Halstead metrics based on file extension."""

		# Common operators for most C-like languages
		common_operators = [
			# Arithmetic
			'+', '-', '*', '/', '%', '++', '--',
			# Assignment
			'=', '+=', '-=', '*=', '/=', '%=',
			# Comparison
			'==', '!=', '<', '>', '<=', '>=',
			# Logical
			'&&', '||', '!',
			# Bitwise
			'&', '|', '^', '~', '<<', '>>',
			# Other
			'?', ':', ';', ',', '.', '->',
			# Brackets and parentheses
			'(', ')', '[', ']', '{', '}',
		]

		# Common operand patterns (identifiers, numbers, strings)
		common_operand_patterns = [
			r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b',  # identifiers
			r'\b(\d+\.?\d*)\b',                # numbers
			r'"([^"]*)"',                      # double-quoted strings
			r"'([^']*)'",                      # single-quoted strings
		]

		if file_extension == '.py':
			operators = common_operators + [
				'and', 'or', 'not', 'in', 'is', 'lambda',
				'if', 'elif', 'else', 'for', 'while', 'def', 'class',
				'import', 'from', 'as', 'try', 'except', 'finally',
				'with', 'yield', 'return', 'pass', 'break', 'continue'
			]

		elif file_extension in ['.js', '.ts', '.jsx', '.tsx']:
			operators = common_operators + [
				'function', 'var', 'let', 'const', 'if', 'else', 'for', 'while',
				'do', 'switch', 'case', 'default', 'break', 'continue',
				'return', 'try', 'catch', 'finally', 'throw', 'new', 'delete',
				'typeof', 'instanceof', 'this', '=>'
			]

		elif file_extension in ['.java', '.scala', '.kt']:
			operators = common_operators + [
				'class', 'interface', 'extends', 'implements', 'public', 'private',
				'protected', 'static', 'final', 'abstract', 'synchronized',
				'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
				'break', 'continue', 'return', 'try', 'catch', 'finally',
				'throw', 'throws', 'new', 'instanceof'
			]

		elif file_extension in ['.cpp', '.c', '.cc', '.cxx', '.h', '.hpp']:
			operators = common_operators + [
				'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
				'break', 'continue', 'return', 'goto', 'sizeof',
				'struct', 'union', 'enum', 'typedef', 'static', 'extern',
				'const', 'volatile', 'auto', 'register'
			]

		elif file_extension == '.go':
			operators = common_operators + [
				'func', 'var', 'const', 'type', 'struct', 'interface',
				'if', 'else', 'for', 'switch', 'case', 'default',
				'break', 'continue', 'return', 'go', 'select',
				'package', 'import', 'defer'
			]

		elif file_extension == '.rs':
			operators = common_operators + [
				'fn', 'let', 'mut', 'const', 'static', 'struct', 'enum', 'trait',
				'impl', 'if', 'else', 'for', 'while', 'loop', 'match',
				'break', 'continue', 'return', 'pub', 'use', 'mod'
			]
		else:
			# Default to common operators
			operators = common_operators

		return operators, common_operand_patterns

	def _remove_comments_and_strings(self, content, file_extension):
		"""Remove comments and string literals to avoid counting them in Halstead metrics."""

		if file_extension == '.py':
			# Remove Python comments and strings
			content = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
			content = re.sub(r'""".*?"""', '', content, flags=re.DOTALL)
			content = re.sub(r"'''.*?'''", '', content, flags=re.DOTALL)
			content = re.sub(r'"[^"]*"', '', content)
			content = re.sub(r"'[^']*'", '', content)

		elif file_extension in ['.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.kt', '.cpp', '.c', '.cc', '.cxx', '.h', '.hpp', '.go', '.rs']:
			# Remove C-style comments and strings
			content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
			content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
			content = re.sub(r'"[^"]*"', '', content)
			content = re.sub(r"'[^']*'", '', content)

		return content

	def _calculate_mccabe_complexity(self, content, file_extension):
		"""Calculate McCabe Cyclomatic Complexity v(G).
		
		Formula: v(G) = #binaryDecision + 1
		Also: v(G) = #IFs + #LOOPs + 1
		"""

		# Remove comments and strings to avoid false positives
		cleaned_content = self._remove_comments_and_strings(content, file_extension)

		# Language-specific binary decision patterns (IF statements and LOOPS)
		if_patterns = []
		loop_patterns = []

		if file_extension in ['.py']:
			# Python patterns
			if_patterns = [r'\bif\b', r'\belif\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b']
		elif file_extension in ['.js', '.ts', '.jsx', '.tsx']:
			# JavaScript/TypeScript patterns
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b', r'\bdo\b']
		elif file_extension in ['.java', '.scala', '.kt']:
			# Java/Scala/Kotlin patterns
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b', r'\bdo\b']
		elif file_extension in ['.cpp', '.c', '.cc', '.cxx', '.h', '.hpp']:
			# C/C++ patterns
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b', r'\bdo\b']
		elif file_extension in ['.go']:
			# Go patterns
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bfor\b']
		elif file_extension in ['.rs']:
			# Rust patterns
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b', r'\bloop\b']
		else:
			# Generic patterns for other languages
			if_patterns = [r'\bif\b', r'\belse\s+if\b']
			loop_patterns = [r'\bwhile\b', r'\bfor\b', r'\bdo\b']

		# Count IF statements
		if_count = 0
		for pattern in if_patterns:
			matches = re.findall(pattern, cleaned_content, re.IGNORECASE)
			if_count += len(matches)

		# Count LOOP statements
		loop_count = 0
		for pattern in loop_patterns:
			matches = re.findall(pattern, cleaned_content, re.IGNORECASE)
			loop_count += len(matches)

		# Additional binary decisions: switch/case, try/catch, ternary operators, logical operators
		additional_decisions = 0

		# Switch/case statements
		if file_extension in ['.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.kt', '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp']:
			case_matches = re.findall(r'\bcase\b', cleaned_content, re.IGNORECASE)
			additional_decisions += len(case_matches)

		# Ternary operators
		ternary_matches = re.findall(r'\?.*:', cleaned_content)
		additional_decisions += len(ternary_matches)

		# Logical operators (each && or || creates a binary decision)
		logical_matches = re.findall(r'&&|\|\|', cleaned_content)
		additional_decisions += len(logical_matches)

		# Exception handling
		if file_extension in ['.py']:
			except_matches = re.findall(r'\bexcept\b', cleaned_content, re.IGNORECASE)
			additional_decisions += len(except_matches)
		elif file_extension in ['.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.kt', '.c', '.cpp', '.cc', '.cxx']:
			catch_matches = re.findall(r'\bcatch\b', cleaned_content, re.IGNORECASE)
			additional_decisions += len(catch_matches)

		# Total binary decisions = IFs + LOOPs + additional decisions
		binary_decisions = if_count + loop_count + additional_decisions

		# McCabe complexity v(G) = #binaryDecision + 1
		complexity = binary_decisions + 1

		# Interpret complexity level (recommendations: function ≲ 15; file ≲ 100)
		if complexity <= 15:
			interpretation = 'simple'
		elif complexity <= 25:
			interpretation = 'moderate'
		elif complexity <= 50:
			interpretation = 'complex'
		else:
			interpretation = 'very_complex'

		return {
			'cyclomatic_complexity': complexity,
			'binary_decisions': binary_decisions,
			'if_count': if_count,
			'loop_count': loop_count,
			'additional_decisions': additional_decisions,
			'interpretation': interpretation
		}

	def _calculate_maintainability_index(self, loc_metrics, halstead_metrics, mccabe_metrics):
		"""Calculate Maintainability Index using LOC, Halstead, and McCabe metrics.
		
		Formula from slides:
		MIwoc = 171 - 5.2 * ln(aveV) - 0.23 * aveG - 16.2 * ln(aveLOC)
		MIcw = 50 * sin(√(2.4 * perCM))
		MI = MIwoc + MIcw
		"""
		import math

		try:
			# Extract required metrics - these are per module (file) averages
			ave_v = halstead_metrics['V'] if halstead_metrics['V'] > 0 else 1  # Halstead Volume per module
			ave_g = mccabe_metrics['cyclomatic_complexity']  # Cyclomatic complexity v(G) per module
			ave_loc = loc_metrics['loc_phy']  # Physical LOC per module
			per_cm = loc_metrics['comment_ratio']  # Comment ratio as percentage (0-100)

			# MIwoc = 171 - 5.2 * ln(aveV) - 0.23 * aveG - 16.2 * ln(aveLOC)
			mi_woc = (171 -
					  5.2 * math.log(max(ave_v, 1)) -
					  0.23 * ave_g -
					  16.2 * math.log(max(ave_loc, 1)))

			# MIcw = 50 * sin(√(2.4 * perCM)) where perCM is percentage
			mi_cw = 50 * math.sin(math.sqrt(2.4 * per_cm)) if per_cm >= 0 else 0

			# MI = MIwoc + MIcw
			maintainability_index = mi_woc + mi_cw

			# Store raw value for analysis, but clamp for display
			raw_maintainability_index = maintainability_index
			# Normalize to 0-171 range (standard MI range)
			maintainability_index = max(0, min(171, maintainability_index))

			return {
				'mi': maintainability_index,
				'mi_raw': raw_maintainability_index,
				'mi_woc': mi_woc,
				'mi_cw': mi_cw,
				'interpretation': self._interpret_maintainability_index(raw_maintainability_index)
			}

		except (ValueError, OverflowError, ZeroDivisionError) as e:
			if conf['debug']:
				print(f'Warning: Maintainability Index calculation failed: {e}')
				print(f'  Input values - loc_metrics: {loc_metrics}')
				print(f'  Input values - halstead_metrics: {halstead_metrics}')
				print(f'  Input values - mccabe_metrics: {mccabe_metrics}')
			return {
				'mi': 0.0,
				'mi_woc': 0.0,
				'mi_cw': 0.0,
				'interpretation': 'calculation_failed'
			}

	def _interpret_maintainability_index(self, mi):
		"""Interpret Maintainability Index score."""
		if mi >= 85:
			return 'good'          # Good maintainability
		elif mi >= 65:
			return 'moderate'      # Moderate maintainability
		elif mi >= 0:
			return 'difficult'     # Difficult to maintain
		else:
			return 'critical'      # Critical/pathological case

	def _calculate_oop_metrics(self, content, file_extension, filepath):
		"""Calculate Object-Oriented Programming software metrics.
		
		Calculates:
		- Efferent Coupling (Ce): Number of classes this class depends on
		- Afferent Coupling (Ca): Number of classes that depend on this class  
		- Instability (I): Ce / (Ce + Ca)
		- Abstractness (A): Abstract classes / Total classes
		- Distance from Main Sequence (D): |A + I - 1|
		"""

		# Initialize OOP metrics
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'instability': 0.0,
			'abstractness': 0.0,
			'distance_main_sequence': 0.0,
			'inheritance_depth': 0,
			'method_count': 0,
			'attribute_count': 0
		}

		if not content.strip():
			return metrics

		try:
			# Remove comments and strings for accurate analysis
			cleaned_content = self._remove_comments_and_strings(content, file_extension)

			# Language-specific OOP analysis
			if file_extension in ['.java', '.scala', '.kt']:
				metrics.update(self._analyze_java_oop_metrics(cleaned_content))
			elif file_extension in ['.py', '.pyi']:
				metrics.update(self._analyze_python_oop_metrics(cleaned_content))
			elif file_extension in ['.cpp', '.cc', '.cxx', '.hpp', '.hxx', '.h']:
				metrics.update(self._analyze_cpp_oop_metrics(cleaned_content))
			elif file_extension in ['.js', '.ts', '.jsx', '.tsx']:
				metrics.update(self._analyze_javascript_oop_metrics(cleaned_content))
			elif file_extension in ['.swift']:
				metrics.update(self._analyze_swift_oop_metrics(cleaned_content))
			elif file_extension in ['.go']:
				metrics.update(self._analyze_go_oop_metrics(cleaned_content))
			elif file_extension in ['.rs']:
				metrics.update(self._analyze_rust_oop_metrics(cleaned_content))

			# Calculate derived metrics
			if metrics['classes_defined'] > 0:
				metrics['abstractness'] = metrics['abstract_classes'] / metrics['classes_defined']

			ce = metrics['efferent_coupling']
			ca = metrics['afferent_coupling']
			if (ce + ca) > 0:
				metrics['instability'] = ce / (ce + ca)

			# Distance from Main Sequence: D = |A + I - 1|
			metrics['distance_main_sequence'] = abs(metrics['abstractness'] + metrics['instability'] - 1.0)

			# Add overall coupling metric (sum of efferent and afferent coupling)
			metrics['coupling'] = metrics['efferent_coupling'] + metrics['afferent_coupling']

		except Exception as e:
			if conf['debug']:
				print(f'Warning: OOP metrics calculation failed for {filepath}: {e}')

		return metrics

	def _analyze_java_oop_metrics(self, content):
		"""Analyze OOP metrics for Java/Scala/Kotlin files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count classes (including inner classes)
		class_patterns = [
			r'\bclass\s+\w+',
			r'\benum\s+\w+',
			r'\b@interface\s+\w+'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['classes_defined'] += len(matches)

		# Count abstract classes
		abstract_patterns = [
			r'\babstract\s+class\s+\w+',
			r'\babstract\s+.*\s+class\s+\w+'
		]
		for pattern in abstract_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['abstract_classes'] += len(matches)

		# Count interfaces
		interface_patterns = [r'\binterface\s+\w+']
		for pattern in interface_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['interfaces_defined'] += len(matches)
			metrics['abstract_classes'] += len(matches)  # Interfaces are abstract

		# Count methods (public, private, protected)
		method_patterns = [
			r'\b(public|private|protected|static).*\s+\w+\s*\([^)]*\)\s*\{',
			r'\b\w+\s*\([^)]*\)\s*\{'  # Basic method pattern
		]
		for pattern in method_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['method_count'] += len(matches)

		# Count attributes/fields
		field_patterns = [
			r'\b(public|private|protected|static)\s+[\w<>,\[\]]+\s+\w+\s*[=;]',
			r'\bprivate\s+[\w<>,\[\]]+\s+\w+',
			r'\bpublic\s+[\w<>,\[\]]+\s+\w+',
			r'\bprotected\s+[\w<>,\[\]]+\s+\w+'
		]
		for pattern in field_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['attribute_count'] += len(matches)

		# Estimate coupling by counting imports and new object creations
		import_matches = re.findall(r'\bimport\s+[\w.]+', content)
		new_matches = re.findall(r'\bnew\s+\w+\s*\(', content)
		metrics['efferent_coupling'] = len(set(import_matches)) + len(new_matches)

		return metrics

	def _analyze_python_oop_metrics(self, content):
		"""Analyze OOP metrics for Python files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count classes
		class_matches = re.findall(r'^class\s+\w+.*:', content, re.MULTILINE)
		metrics['classes_defined'] = len(class_matches)

		# Count abstract classes (ABC or abstractmethod)
		abstract_patterns = [
			r'from\s+abc\s+import',
			r'@abstractmethod',
			r'ABC\)',
			r'class.*ABC.*:'
		]
		has_abc = any(re.search(pattern, content) for pattern in abstract_patterns)
		if has_abc and metrics['classes_defined'] > 0:
			metrics['abstract_classes'] = 1  # Conservative estimate

		# Count methods (def within classes)
		method_matches = re.findall(r'^\s+def\s+\w+\s*\(.*\):', content, re.MULTILINE)
		metrics['method_count'] = len(method_matches)

		# Count attributes (self.attribute assignments)
		attribute_matches = re.findall(r'self\.\w+\s*=', content)
		metrics['attribute_count'] = len(set(attribute_matches))

		# Estimate coupling by counting imports
		import_matches = re.findall(r'^(?:from\s+[\w.]+\s+)?import\s+[\w.,\s]+', content, re.MULTILINE)
		metrics['efferent_coupling'] = len(import_matches)

		return metrics

	def _analyze_cpp_oop_metrics(self, content):
		"""Analyze OOP metrics for C++ files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count classes and structs
		class_patterns = [
			r'\bclass\s+\w+',
			r'\bstruct\s+\w+'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content)
			metrics['classes_defined'] += len(matches)

		# Count abstract classes (virtual methods)
		virtual_matches = re.findall(r'virtual\s+.*\s*=\s*0\s*;', content)
		if virtual_matches:
			metrics['abstract_classes'] = 1  # Conservative estimate

		# Count methods (function definitions in classes)
		method_patterns = [
			r'\b\w+\s*\([^)]*\)\s*\{',
			r'\b(public|private|protected):\s*\n\s*\w+\s*\([^)]*\)'
		]
		for pattern in method_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['method_count'] += len(matches)

		# Count attributes (member variables)
		member_patterns = [
			r'\b(public|private|protected):\s*\n\s*[\w<>,\*&\[\]]+\s+\w+\s*;',
			r'^\s*[\w<>,\*&\[\]]+\s+\w+\s*;',
		]
		for pattern in member_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['attribute_count'] += len(matches)

		# Estimate coupling by counting includes
		include_matches = re.findall(r'#include\s*[<"][\w./]+[>"]', content)
		metrics['efferent_coupling'] = len(include_matches)

		return metrics

	def _analyze_javascript_oop_metrics(self, content):
		"""Analyze OOP metrics for JavaScript/TypeScript files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count classes
		class_matches = re.findall(r'\bclass\s+\w+', content)
		metrics['classes_defined'] = len(class_matches)

		# Count interfaces (TypeScript)
		interface_matches = re.findall(r'\binterface\s+\w+', content)
		metrics['interfaces_defined'] = len(interface_matches)
		metrics['abstract_classes'] += len(interface_matches)

		# Count abstract classes (TypeScript)
		abstract_matches = re.findall(r'\babstract\s+class\s+\w+', content)
		metrics['abstract_classes'] += len(abstract_matches)

		# Count methods
		method_patterns = [
			r'\b\w+\s*\([^)]*\)\s*\{',
			r'\b\w+:\s*\([^)]*\)\s*=>'
		]
		for pattern in method_patterns:
			matches = re.findall(pattern, content)
			metrics['method_count'] += len(matches)

		# Count properties/attributes
		property_patterns = [
			r'this\.\w+\s*=',
			r'\b\w+:\s*[\w\[\]<>]+\s*[;,]'
		]
		for pattern in property_patterns:
			matches = re.findall(pattern, content)
			metrics['attribute_count'] += len(matches)

		# Estimate coupling by counting imports/requires
		import_patterns = [
			r'import\s+.*\s+from\s+["\'][\w./]+["\']',
			r'require\s*\(["\'][\w./]+["\']\)'
		]
		for pattern in import_patterns:
			matches = re.findall(pattern, content)
			metrics['efferent_coupling'] += len(matches)

		return metrics

	def _analyze_swift_oop_metrics(self, content):
		"""Analyze OOP metrics for Swift files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count classes and structs
		class_patterns = [
			r'\bclass\s+\w+',
			r'\bstruct\s+\w+'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content)
			metrics['classes_defined'] += len(matches)

		# Count protocols (Swift's interfaces)
		protocol_matches = re.findall(r'\bprotocol\s+\w+', content)
		metrics['interfaces_defined'] = len(protocol_matches)
		metrics['abstract_classes'] += len(protocol_matches)

		# Count methods/functions
		method_matches = re.findall(r'\bfunc\s+\w+\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count properties
		property_patterns = [
			r'\bvar\s+\w+\s*:',
			r'\blet\s+\w+\s*:'
		]
		for pattern in property_patterns:
			matches = re.findall(pattern, content)
			metrics['attribute_count'] += len(matches)

		# Estimate coupling by counting imports
		import_matches = re.findall(r'\bimport\s+\w+', content)
		metrics['efferent_coupling'] = len(import_matches)

		return metrics

	def _analyze_go_oop_metrics(self, content):
		"""Analyze OOP-like metrics for Go files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count structs (Go's equivalent to classes)
		struct_matches = re.findall(r'\btype\s+\w+\s+struct\s*\{', content)
		metrics['classes_defined'] = len(struct_matches)

		# Count interfaces
		interface_matches = re.findall(r'\btype\s+\w+\s+interface\s*\{', content)
		metrics['interfaces_defined'] = len(interface_matches)
		metrics['abstract_classes'] = len(interface_matches)

		# Count methods (functions with receivers)
		method_matches = re.findall(r'\bfunc\s*\([^)]*\)\s*\w+\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count struct fields
		# This is a simple approximation - count field-like declarations in structs
		field_matches = re.findall(r'^\s*\w+\s+[\w\[\]\*]+\s*$', content, re.MULTILINE)
		metrics['attribute_count'] = len(field_matches)

		# Estimate coupling by counting imports
		import_matches = re.findall(r'\bimport\s+["\w/.-]+', content)
		metrics['efferent_coupling'] = len(import_matches)

		return metrics

	def _analyze_rust_oop_metrics(self, content):
		"""Analyze OOP-like metrics for Rust files."""

		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'inheritance_depth': 0
		}

		# Count structs and enums (Rust's data types)
		struct_matches = re.findall(r'\bstruct\s+\w+', content)
		enum_matches = re.findall(r'\benum\s+\w+', content)
		metrics['classes_defined'] = len(struct_matches) + len(enum_matches)

		# Count traits (Rust's interfaces)
		trait_matches = re.findall(r'\btrait\s+\w+', content)
		metrics['interfaces_defined'] = len(trait_matches)
		metrics['abstract_classes'] = len(trait_matches)

		# Count impl methods
		method_matches = re.findall(r'\bfn\s+\w+\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count struct fields (simplified)
		field_matches = re.findall(r'^\s*\w+\s*:\s*[\w<>,\[\]]+\s*,?', content, re.MULTILINE)
		metrics['attribute_count'] = len(field_matches)

		# Estimate coupling by counting use statements
		use_matches = re.findall(r'\buse\s+[\w:]+', content)
		extern_matches = re.findall(r'\bextern\s+crate\s+\w+', content)
		metrics['efferent_coupling'] = len(use_matches) + len(extern_matches)

		return metrics

	def get_repository_files_for_mi(self, repository_path=None):
		"""Get all files from repository that match allowed extensions for MI calculation.
		
		Args:
			repository_path: Path to repository (if None, uses current working directory)
		
		Returns:
			list: List of file paths that match allowed extensions
		"""
		original_cwd = os.getcwd() if repository_path else None
		if repository_path:
			try:
				os.chdir(repository_path)
			except OSError as e:
				print(f'Warning: Could not change to repository path {repository_path}: {e}')
				return []

		try:
			files_output = getpipeoutput(['git ls-files'])
			if not files_output.strip():
				print(f'    No files found in repository: {repository_path or os.getcwd()}')
				return []

			source_files = []
			print(f'  📁 Scanning repository: {repository_path or os.getcwd()}')
			print('  🔍 Files matching allowed extensions:')

			for filepath in files_output.strip().split('\n'):
				if filepath.strip():
					filename = filepath.split('/')[-1]
					if should_include_file(filename):
						full_path = os.path.join(os.getcwd(), filepath)
						if os.path.exists(full_path):
							source_files.append(filepath)
							# Output the found files to CLI as requested
							ext = os.path.splitext(filename)[1] or 'no-ext'
							print(f'      ✓ {filepath} ({ext})')

			print(f'  📊 Found {len(source_files)} files for MI analysis')
			return source_files

		except Exception as e:
			print(f'Warning: Failed to get repository files: {e}')
			return []
		finally:
			if repository_path and original_cwd:
				try:
					os.chdir(original_cwd)
				except OSError:
					pass

	def calculate_mi_for_repository(self, repository_path=None):
		"""Calculate MI for all matching files in a specific repository.
		
		Args:
			repository_path: Path to repository (if None, uses current working directory)
			
		Returns:
			dict: MI analysis results for the repository
		"""
		print('\n🧮 Calculating Maintainability Index (MI) for repository...')

		original_cwd = os.getcwd() if repository_path else None
		if repository_path:
			try:
				os.chdir(repository_path)
				print(f'  📁 Repository: {repository_path}')
			except OSError as e:
				print(f'Error: Could not access repository {repository_path}: {e}')
				return None

		try:
			# Get all source files for this repository
			source_files = self.get_repository_files_for_mi(repository_path)

			if not source_files:
				print('    ⚠️  No source files found with allowed extensions')
				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': 0,
					'mi_results': [],
					'summary': 'No files found'
				}

			# Calculate MI for each file
			mi_results = []
			successful_calculations = 0

			print(f'\n  🔬 Calculating MI for {len(source_files)} files...')

			for i, filepath in enumerate(source_files):
				try:
					metrics = self.calculate_comprehensive_metrics(filepath)
					if metrics and 'maintainability_index' in metrics:
						mi_data = metrics['maintainability_index']
						file_result = {
							'filepath': filepath,
							'extension': metrics.get('extension', ''),
							'mi_score': mi_data['mi'],
							'mi_raw': mi_data['mi_raw'],
							'interpretation': mi_data['interpretation'],
							'loc_phy': metrics['loc']['loc_phy'],
							'complexity': metrics['mccabe']['cyclomatic_complexity']
						}
						mi_results.append(file_result)
						successful_calculations += 1

						# Output MI result to CLI
						print(f'      📄 {filepath}: MI = {mi_data["mi"]:.1f} ({mi_data["interpretation"]})')

				except Exception as e:
					if conf['debug']:
						print(f'      ⚠️  Failed to calculate MI for {filepath}: {e}')

			# Calculate summary statistics
			if mi_results:
				mi_scores = [result['mi_score'] for result in mi_results]
				avg_mi = sum(mi_scores) / len(mi_scores)

				# Count by interpretation
				interpretation_counts = {}
				for result in mi_results:
					interp = result['interpretation']
					interpretation_counts[interp] = interpretation_counts.get(interp, 0) + 1

				print('\n  📊 MI Analysis Summary:')
				print(f'      Files analyzed: {len(mi_results)}')
				print(f'      Average MI: {avg_mi:.1f}')
				print('      Distribution:')
				for interp, count in interpretation_counts.items():
					print(f'        - {interp}: {count} files')

				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': len(mi_results),
					'successful_calculations': successful_calculations,
					'mi_results': mi_results,
					'summary': {
						'average_mi': avg_mi,
						'distribution': interpretation_counts,
						'total_files': len(source_files),
						'calculated_files': len(mi_results)
					}
				}
			else:
				print('    ⚠️  No successful MI calculations')
				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': 0,
					'mi_results': [],
					'summary': 'No successful calculations'
				}

		except Exception as e:
			print(f'Error during MI calculation: {e}')
			return None
		finally:
			if repository_path and original_cwd:
				try:
					os.chdir(original_cwd)
				except OSError:
					pass

	def calculate_mccabe_for_repository(self, repository_path=None):
		"""Calculate McCabe complexity metrics for all files in the repository."""
		print('🔄 Calculating McCabe Complexity for repository...')
		print(f'  📁 Repository: {repository_path or os.getcwd()}')

		original_cwd = os.getcwd()

		try:
			source_files = self.get_repository_files_for_mi(repository_path)

			if not source_files:
				print('    ⚠️  No source files found for McCabe analysis')
				return None

			print(f'  📊 Found {len(source_files)} files for McCabe analysis')
			print(f'  🔬 Calculating complexity for {len(source_files)} files...')

			complexity_results = []

			for filepath in source_files:
				try:
					full_filepath = os.path.join(repository_path or os.getcwd(), filepath)
					with open(full_filepath, encoding='utf-8', errors='ignore') as f:
						content = f.read()

					ext = os.path.splitext(filepath)[1].lower()
					mccabe_metrics = self._calculate_mccabe_complexity(content, ext)
					complexity = mccabe_metrics['cyclomatic_complexity']

					# Categorize complexity
					if complexity <= 5:
						category = 'simple'
					elif complexity <= 10:
						category = 'moderate'
					elif complexity <= 20:
						category = 'complex'
					else:
						category = 'very_complex'

					result = {
						'filepath': filepath,
						'complexity': complexity,
						'category': category
					}
					complexity_results.append(result)

					print(f'      📄 {filepath}: Complexity = {complexity} ({category})')

				except Exception as e:
					print(f'      ❌ Error analyzing {filepath}: {e}')

			if complexity_results:
				# Calculate summary statistics
				complexities = [r['complexity'] for r in complexity_results]
				avg_complexity = sum(complexities) / len(complexities)
				max_complexity = max(complexities)

				# Count by category
				simple_files = len([r for r in complexity_results if r['category'] == 'simple'])
				moderate_files = len([r for r in complexity_results if r['category'] == 'moderate'])
				complex_files = len([r for r in complexity_results if r['category'] == 'complex'])
				very_complex_files = len([r for r in complexity_results if r['category'] == 'very_complex'])

				print('\n  📊 McCabe Complexity Analysis Summary:')
				print(f'      Files analyzed: {len(complexity_results)}')
				print(f'      Average complexity: {avg_complexity:.1f}')
				print(f'      Maximum complexity: {max_complexity}')
				print('      Distribution:')
				print(f'        - Simple (≤5): {simple_files} files')
				print(f'        - Moderate (6-10): {moderate_files} files')
				print(f'        - Complex (11-20): {complex_files} files')
				print(f'        - Very Complex (>20): {very_complex_files} files')

				# Show all files by complexity (highest first)
				print('\n  === Files by McCabe Complexity (Highest First) ===')
				sorted_results = sorted(complexity_results, key=lambda x: x['complexity'], reverse=True)
				for result in sorted_results:  # Show all files
					if result['complexity'] > 20:
						print(f'    🚨 {result["filepath"]} (Complexity: {result["complexity"]}) - Very Complex')
					elif result['complexity'] > 10:
						print(f'    ⚠️  {result["filepath"]} (Complexity: {result["complexity"]}) - Complex')
					elif result['complexity'] > 5:
						print(f'    � {result["filepath"]} (Complexity: {result["complexity"]}) - Moderate')
					else:
						print(f'    ✅ {result["filepath"]} (Complexity: {result["complexity"]}) - Simple')

				print('✓ McCabe complexity calculation completed')
				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': len(complexity_results),
					'results': complexity_results,
					'summary': {
						'average_complexity': avg_complexity,
						'max_complexity': max_complexity,
						'simple_files': simple_files,
						'moderate_files': moderate_files,
						'complex_files': complex_files,
						'very_complex_files': very_complex_files
					}
				}
			else:
				print('    ⚠️  No successful McCabe calculations')
				return None

		except Exception as e:
			print(f'Error during McCabe calculation: {e}')
			return None
		finally:
			if repository_path:
				try:
					os.chdir(original_cwd)
				except OSError:
					pass

	def calculate_halstead_for_repository(self, repository_path=None):
		"""Calculate Halstead metrics for all files in the repository."""
		print('🧮 Calculating Halstead Metrics for repository...')
		print(f'  📁 Repository: {repository_path or os.getcwd()}')

		original_cwd = os.getcwd()

		try:
			source_files = self.get_repository_files_for_mi(repository_path)

			if not source_files:
				print('    ⚠️  No source files found for Halstead analysis')
				return None

			print(f'  📊 Found {len(source_files)} files for Halstead analysis')
			print(f'  🔬 Calculating metrics for {len(source_files)} files...')

			halstead_results = []

			for filepath in source_files:
				try:
					full_filepath = os.path.join(repository_path or os.getcwd(), filepath)
					with open(full_filepath, encoding='utf-8', errors='ignore') as f:
						content = f.read()

					ext = os.path.splitext(filepath)[1].lower()
					halstead_metrics = self._calculate_halstead_metrics(content, ext)

					result = {
						'filepath': filepath,
						'volume': halstead_metrics['V'],
						'difficulty': halstead_metrics['D'],
						'effort': halstead_metrics['E'],
						'bugs': halstead_metrics['B'],
						'time': halstead_metrics['T']
					}
					halstead_results.append(result)

					print(f'      📄 {filepath}: Volume={halstead_metrics["V"]:.1f}, Difficulty={halstead_metrics["D"]:.1f}, Effort={halstead_metrics["E"]:.1f}')

				except Exception as e:
					print(f'      ❌ Error analyzing {filepath}: {e}')

			if halstead_results:
				# Calculate summary statistics
				volumes = [r['volume'] for r in halstead_results]
				difficulties = [r['difficulty'] for r in halstead_results]
				efforts = [r['effort'] for r in halstead_results]
				bugs = [r['bugs'] for r in halstead_results]

				avg_volume = sum(volumes) / len(volumes)
				avg_difficulty = sum(difficulties) / len(difficulties)
				avg_effort = sum(efforts) / len(efforts)
				total_bugs = sum(bugs)

				print('\n  📊 Halstead Metrics Analysis Summary:')
				print(f'      Files analyzed: {len(halstead_results)}')
				print(f'      Average volume: {avg_volume:.1f}')
				print(f'      Average difficulty: {avg_difficulty:.1f}')
				print(f'      Average effort: {avg_effort:.1f}')
				print(f'      Estimated total bugs: {total_bugs:.2f}')

				# Show all files by effort (highest first)
				print('\n  === Files by Halstead Effort (Highest First) ===')
				sorted_results = sorted(halstead_results, key=lambda x: x['effort'], reverse=True)
				for result in sorted_results:  # Show all files
					print(f'    📄 {result["filepath"]} (Effort: {result["effort"]:.1f}, Bugs: {result["bugs"]:.2f})')

				print('✓ Halstead metrics calculation completed')
				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': len(halstead_results),
					'results': halstead_results,
					'summary': {
						'average_volume': avg_volume,
						'average_difficulty': avg_difficulty,
						'average_effort': avg_effort,
						'total_estimated_bugs': total_bugs
					}
				}
			else:
				print('    ⚠️  No successful Halstead calculations')
				return None

		except Exception as e:
			print(f'Error during Halstead calculation: {e}')
			return None
		finally:
			if repository_path:
				try:
					os.chdir(original_cwd)
				except OSError:
					pass

	def calculate_oop_for_repository(self, repository_path=None):
		"""Calculate OOP metrics for all files in the repository."""
		print('🏗️ Calculating OOP Metrics for repository...')
		print(f'  📁 Repository: {repository_path or os.getcwd()}')

		original_cwd = os.getcwd()

		try:
			source_files = self.get_repository_files_for_mi(repository_path)

			if not source_files:
				print('    ⚠️  No source files found for OOP analysis')
				return None

			print(f'  📊 Found {len(source_files)} files for OOP analysis')
			print(f'  🔬 Calculating metrics for {len(source_files)} files...')

			oop_results = []
			total_ce = 0
			total_ca = 0
			total_abstract = 0

			for filepath in source_files:
				try:
					full_filepath = os.path.join(repository_path or os.getcwd(), filepath)
					with open(full_filepath, encoding='utf-8', errors='ignore') as f:
						content = f.read()

					ext = os.path.splitext(filepath)[1].lower()
					oop_metrics = self._calculate_oop_metrics(content, ext, full_filepath)
					total_ce += oop_metrics.get('efferent_coupling', 0)
					total_ca += oop_metrics.get('afferent_coupling', 0)
					total_abstract += oop_metrics.get('abstract_classes', 0)

					result = {
						'filepath': filepath,
						'classes': oop_metrics['classes_defined'],
						'abstract_classes': oop_metrics['abstract_classes'],
						'interfaces': oop_metrics['interfaces_defined'],
						'methods': oop_metrics['method_count'],
						'attributes': oop_metrics['attribute_count'],
						'inheritance_depth': oop_metrics['inheritance_depth'],
						'coupling': oop_metrics['coupling']
					}
					oop_results.append(result)

					if oop_metrics['classes_defined'] > 0:
						print(f'      📄 {filepath}: Classes={oop_metrics["classes_defined"]}, Methods={oop_metrics["method_count"]}, Coupling={oop_metrics["coupling"]:.1f}')
					else:
						print(f'      📄 {filepath}: No OOP constructs found')

				except Exception as e:
					print(f'      ❌ Error analyzing {filepath}: {e}')

			if oop_results:
				# Calculate summary statistics
				total_classes = sum(r['classes'] for r in oop_results)
				total_methods = sum(r['methods'] for r in oop_results)
				total_attributes = sum(r['attributes'] for r in oop_results)
				files_with_oop = len([r for r in oop_results if r['classes'] > 0])

				avg_coupling = sum(r['coupling'] for r in oop_results if r['coupling'] > 0)
				if files_with_oop > 0:
					avg_coupling = avg_coupling / files_with_oop
				else:
					avg_coupling = 0

				abstractness = total_abstract / total_classes if total_classes > 0 else 0.0
				instability = total_ce / (total_ce + total_ca) if (total_ce + total_ca) > 0 else 0.0
				distance = abs(abstractness + instability - 1.0)

				self.oop_metrics = {
					'total_classes': total_classes,
					'abstract_classes': total_abstract,
					'abstractness': abstractness,
					'instability': instability,
					'distance': distance
				}

				print('\n  📊 OOP Metrics Analysis Summary:')
				print(f'      Files analyzed: {len(oop_results)}')
				print(f'      Files with OOP constructs: {files_with_oop}')
				print(f'      Total classes: {total_classes}')
				print(f'      Total methods: {total_methods}')
				print(f'      Total attributes: {total_attributes}')
				print(f'      Average coupling: {avg_coupling:.1f}')

				# Show all files with OOP constructs by coupling (highest first)
				if files_with_oop > 0:
					print('\n  === Files with OOP Constructs (by Coupling) ===')
					sorted_results = sorted([r for r in oop_results if r['coupling'] > 0],
											key=lambda x: x['coupling'], reverse=True)
					for result in sorted_results:  # Show all files with OOP
						print(f'    📄 {result["filepath"]} (Classes: {result["classes"]}, Methods: {result["methods"]}, Coupling: {result["coupling"]:.1f})')

				print('✓ OOP metrics calculation completed')
				return {
					'repository_path': repository_path or os.getcwd(),
					'files_analyzed': len(oop_results),
					'results': oop_results,
					'summary': {
						'total_classes': total_classes,
						'total_methods': total_methods,
						'total_attributes': total_attributes,
						'files_with_oop': files_with_oop,
						'average_coupling': avg_coupling
					}
				}
			else:
				self.oop_metrics = {}
				print('    ⚠️  No successful OOP calculations')
				return None

		except Exception as e:
			print(f'Error during OOP calculation: {e}')
			return None
		finally:
			if repository_path:
				try:
					os.chdir(original_cwd)
				except OSError:
					pass

	def _calculate_comprehensive_project_metrics(self, repository_path=None):
		"""Calculate comprehensive code quality metrics for the entire project."""
		print('  Analyzing source files for comprehensive metrics...')

		# Get all source files using the new method - use the target repository path
		source_files = self.get_repository_files_for_mi(repository_path)

		if not source_files:
			print('    No source files found with allowed extensions')
			return

		print(f'    Processing {len(source_files)} source files...')

		try:
			# Initialize storage for per-file metrics (no aggregation)
			file_metrics_dict = {}
			total_complexity = 0
			total_mi = 0.0

			# Initialize aggregate metrics (keeping only what's needed for summaries)
			project_totals = {
				'files_analyzed': 0,
				'files_by_maintainability': {
					'good': 0,
					'moderate': 0,
					'difficult': 0,
					'critical': 0
				},
				'mi_file_details': {
					'good': [],
					'moderate': [],
					'difficult': [],
					'critical': []
				},
				'files_with_oop': 0
			}

			# Analyze each file
			for i, filepath in enumerate(source_files):
				if i % 50 == 0 and i > 0:
					print(f'    Processed {i}/{len(source_files)} files...')

				try:
					# Use full path since we're not in the repository directory
					if repository_path:
						full_filepath = os.path.join(repository_path, filepath)
					else:
						full_filepath = filepath
					metrics = self.calculate_comprehensive_metrics(full_filepath)
					if not metrics:
						continue

					# Store complete file metrics without aggregation
					file_metric_entry = {
						'filepath': filepath,
						'extension': metrics['extension'],
						'loc': metrics['loc'],
						'halstead': metrics['halstead'],
						'mccabe': metrics['mccabe'],
						'complexity': metrics['mccabe'], # Add alias for compatibility
						'maintainability_index': metrics['maintainability_index'],
						'oop': metrics['oop'],
						'complexity_score': metrics['complexity_score']
					}
					file_metrics_dict[filepath] = file_metric_entry

					total_complexity += metrics['mccabe'].get('cyclomatic_complexity', 0)
					total_mi += metrics['maintainability_index'].get('mi', 0.0)

					# Only track maintainability categories for summaries
					mi = metrics['maintainability_index']
					mi_category = mi['interpretation']
					project_totals['files_by_maintainability'][mi_category] += 1

					# Store detailed file information for MI analysis
					file_info = {
						'filepath': filepath,
						'mi_score': mi['mi'],
						'mi_raw': mi['mi_raw'],
						'extension': metrics['extension'],
						'loc': metrics['loc']['loc_phy'],
						'complexity': metrics['mccabe']['cyclomatic_complexity']
					}
					project_totals['mi_file_details'][mi_category].append(file_info)

					# Count files that have OOP constructs
					oop = metrics['oop']
					if (oop['classes_defined'] > 0 or oop['interfaces_defined'] > 0 or
						oop['method_count'] > 0 or oop['attribute_count'] > 0):
						project_totals['files_with_oop'] += 1

					project_totals['files_analyzed'] += 1

				except Exception as e:
					if conf['debug']:
						print(f'    Warning: Failed to analyze {filepath}: {e}')
					continue

			# Calculate averages and store results
			if project_totals['files_analyzed'] > 0:
				files_count = project_totals['files_analyzed']

				# Store comprehensive metrics in project health
				if 'comprehensive_metrics' not in self.project_health:
					self.project_health['comprehensive_metrics'] = {}

				cm = self.project_health['comprehensive_metrics']

				# Store individual file metrics (no aggregation)
				cm['file_metrics'] = file_metrics_dict
				cm['files_analyzed'] = files_count
				cm['total_complexity'] = total_complexity
				cm['average_mi'] = total_mi / files_count

				# Store only maintainability categories for summary
				cm['maintainability_summary'] = {
					'good_files': project_totals['files_by_maintainability']['good'],
					'moderate_files': project_totals['files_by_maintainability']['moderate'],
					'difficult_files': project_totals['files_by_maintainability']['difficult'],
					'critical_files': project_totals['files_by_maintainability']['critical'],
					'files_analyzed': files_count,
					'file_details': project_totals['mi_file_details']
				}

				# Store only OOP file count summary
				cm['oop_summary'] = {
					'files_with_oop': project_totals['files_with_oop'],
					'files_analyzed': files_count
				}

				print(f'    Completed comprehensive analysis of {files_count} files')
				print('    Stored individual file metrics (no aggregation)')

				# Print detailed MI analysis
				mi_summary = cm['maintainability_summary']
				print('\n  === Maintainability Index Analysis by Category ===')
				print(f'    📈 Good Files (MI ≥ 85):       {mi_summary["good_files"]:4d} files')
				print(f'    📊 Moderate Files (65 ≤ MI < 85): {mi_summary["moderate_files"]:4d} files')
				print(f'    📉 Difficult Files (0 ≤ MI < 65): {mi_summary["difficult_files"]:4d} files')
				print(f'    ⚠️  Critical Files (MI < 0):    {mi_summary["critical_files"]:4d} files')
				print(f'    📁 Total Files Analyzed:       {files_count:4d} files')

				# Show most problematic files if any exist
				file_details = mi_summary['file_details']
				if file_details['critical'] or file_details['difficult']:
					print('\n  === Files Requiring Attention ===')

					# Show critical files (MI < 0)
					if file_details['critical']:
						print('    🚨 Critical Files (MI < 0):')
						critical_files = sorted(file_details['critical'], key=lambda x: x['mi_raw'])
						for file_info in critical_files:  # Show all critical files
							print(f'      {file_info["filepath"]} (MI: {file_info["mi_raw"]:.1f}, LOC: {file_info["loc"]}, Complexity: {file_info["complexity"]})')

					# Show worst difficult files (0 ≤ MI < 65)
					if file_details['difficult']:
						print('    ⚠️  Difficult Files (0 ≤ MI < 65):')
						difficult_files = sorted(file_details['difficult'], key=lambda x: x['mi_raw'])
						for file_info in difficult_files:  # Show all difficult files
							print(f'      {file_info["filepath"]} (MI: {file_info["mi_raw"]:.1f}, LOC: {file_info["loc"]}, Complexity: {file_info["complexity"]})')

				# Show best maintained files if any exist
				if file_details['good']:
					print('\n  === Well-Maintained Files ===')
					good_files = sorted(file_details['good'], key=lambda x: x['mi_raw'], reverse=True)
					for file_info in good_files:  # Show all good files
						print(f'    ✅ {file_info["filepath"]} (MI: {file_info["mi_raw"]:.1f}, LOC: {file_info["loc"]}, Complexity: {file_info["complexity"]})')

				# Show extension-based MI analysis
				print('  === Maintainability Index by File Extension ===')
				extension_stats = {}

				# Collect stats by extension
				for category in ['good', 'moderate', 'difficult', 'critical']:
					for file_info in file_details[category]:
						ext = file_info['extension']
						if ext not in extension_stats:
							extension_stats[ext] = {
								'good': 0, 'moderate': 0, 'difficult': 0, 'critical': 0,
								'total': 0, 'sum_mi': 0.0
							}
						extension_stats[ext][category] += 1
						extension_stats[ext]['total'] += 1
						extension_stats[ext]['sum_mi'] += file_info['mi_raw']

				# Display extension statistics
				if extension_stats:
					for ext in sorted(extension_stats.keys()):
						stats = extension_stats[ext]
						avg_mi = stats['sum_mi'] / stats['total'] if stats['total'] > 0 else 0.0
						print(f'    {ext:8s}: {stats["total"]:3d} files (avg MI: {avg_mi:6.1f}) | ' +
							  f'Good: {stats["good"]:2d}, Moderate: {stats["moderate"]:2d}, ' +
							  f'Difficult: {stats["difficult"]:2d}, Critical: {stats["critical"]:2d}')

				print()  # Add spacing after MI analysis
			else:
				print('    No files could be analyzed for comprehensive metrics')

		except Exception as e:
			if conf['debug']:
				print(f'    Error in comprehensive metrics calculation: {e}')



