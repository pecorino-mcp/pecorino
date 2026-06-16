"""
Command-line interface module for Gitstats3.

Contains the GitStats CLI class and usage functions.
"""

import datetime
import gc
import getopt
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from typing import Dict, Any

from .gitstats_config import conf
from .gitstats_export import DateTimeEncoder, MetricsExporter, export_to_json
from .gitstats_gitcommands import (
	get_exectime_external,
	getpipeoutput,
	is_git_repository,
)
from .gitstats_gitdatacollector import GitDataCollector
from .gitstats_helpers import time_start
from .gitstats_hotspot import HotspotDetector
from .gitstats_repository import discover_repositories


def usage():
	print("""
Usage: gitstats [options] <gitpath..> <outputpath>
       gitstats [options] --multi-repo <scan-folder> <outputpath>

Options:
-c key=value     Override configuration value
--debug          Enable debug output
--verbose        Enable verbose output
--multi-repo     Scan folder recursively for multiple repositories and generate reports for each
--no-hotspots    Disable hotspot analysis
--mcp            Start the Model Context Protocol (MCP) server
-h, --help       Show this help message

Note: GitStats generates structured JSON reports with detailed statistics.

Examples:
  gitstats repo output                    # Generates JSON report
  gitstats --verbose repo output          # With verbose output
  gitstats --multi-repo /path/to/repos output  # Generate reports for all repos found recursively
  gitstats --debug -c max_authors=50 repo output
""")


class GitStats:
	def run(self, args_orig):
		self.original_stdout = sys.stdout
		if "--mcp" in args_orig:
			from src.gitstats_mcp import mcp
			mcp.run("stdio")
			return

		# Suppress all standard prints completely
		import os
		sys.stdout = open(os.devnull, 'w')

		multi_repo_mode = False
		analyze_hotspots_enabled = True
		optlist, args = getopt.getopt(args_orig, 'hc:', ["help", "debug", "verbose", "multi-repo", "no-hotspots", "mcp"])
		for o,v in optlist:
			if o == '-c':
				if '=' not in v:
					print(f'FATAL: Invalid configuration format. Use key=value: {v}')
					sys.exit(1)
				key, value = v.split('=', 1)
				if key not in conf:
					raise KeyError('no such key "%s" in config' % key)

				# Validate configuration values
				try:
					if isinstance(conf[key], bool):
						conf[key] = value.lower() in ('true', '1', 'yes', 'on')
					elif isinstance(conf[key], int):
						new_value = int(value)
						if key in ['max_authors', 'max_domains'] and new_value < 1:
							print(f'FATAL: {key} must be at least 1, got: {new_value}')
							sys.exit(1)
						conf[key] = new_value
					elif key.endswith('_patterns') and value:
						# Handle comma-separated patterns
						conf[key] = [pattern.strip() for pattern in value.split(',') if pattern.strip()]
					elif key == 'allowed_extensions' and value:
						# Handle comma-separated extensions, ensure they start with '.'
						extensions = []
						for ext in value.split(','):
							ext = ext.strip()
							if ext and not ext.startswith('.'):
								ext = '.' + ext
							if ext:
								extensions.append(ext)
						conf[key] = set(extensions)
					else:
						conf[key] = value
				except ValueError as e:
					print(f'FATAL: Invalid value for {key}: {value} ({e})')
					sys.exit(1)
			elif o == '--debug':
				conf['debug'] = True
				conf['verbose'] = True  # Debug implies verbose
			elif o == '--verbose':
				conf['verbose'] = True
			elif o == '--multi-repo':
				multi_repo_mode = True
			elif o == '--export-json':
				export_json = True
			elif o == '--export-yaml':
				export_yaml = True
			elif o == '--no-hotspots':
				analyze_hotspots_enabled = False
			elif o in ('-h', '--help'):
				sys.stdout = self.original_stdout
				usage()
				sys.exit()

		# Store export flags in config for use in processing
		conf['export_json'] = True
		conf['export_yaml'] = False
		conf['analyze_hotspots'] = analyze_hotspots_enabled

		if multi_repo_mode:
			if len(args) != 2:
				print('FATAL: --multi-repo requires exactly two arguments: <scan-folder> <outputpath>')
				usage()
				sys.exit(1)

			scan_folder = os.path.abspath(args[0])
			outputpath = os.path.abspath(args[1])

			# Enhanced validation of scan folder
			if not os.path.exists(scan_folder):
				print(f'FATAL: Scan folder does not exist: {scan_folder}')
				sys.exit(1)
			if not os.path.isdir(scan_folder):
				print(f'FATAL: Scan folder is not a directory: {scan_folder}')
				sys.exit(1)
			if not os.access(scan_folder, os.R_OK):
				print(f'FATAL: No read permission for scan folder: {scan_folder}')
				sys.exit(1)

			# Check for multi-repo configuration options
			max_depth = conf.get('multi_repo_max_depth', 10)
			include_patterns = conf.get('multi_repo_include_patterns', None)
			exclude_patterns = conf.get('multi_repo_exclude_patterns', None)

			# Discover repositories with recursive scanning always enabled
			print(f'Scanning folder recursively for git repositories: {scan_folder}')
			print(f'  Maximum scanning depth: {max_depth}')

			try:
				repositories = discover_repositories(
					scan_folder,
					recursive=True,  # Always use recursive scanning
					max_depth=max_depth,
					include_patterns=include_patterns,
					exclude_patterns=exclude_patterns
				)
			except Exception as e:
				print(f'FATAL: Error during repository discovery: {e}')
				if conf['debug']:
					import traceback
					traceback.print_exc()
				sys.exit(1)

			if not repositories:
				print(f'No git repositories found in: {scan_folder}')
				print(f'Searched recursively up to depth {max_depth}')
				sys.exit(0)

			print(f'Found {len(repositories)} git repositories:')
			for repo_name, repo_path, repo_type in repositories:
				type_indicator = f' ({repo_type})' if repo_type != 'regular' else ''
				print(f'  - {repo_name}{type_indicator}')

			# Generate reports for each repository
			self.run_multi_repo(repositories, outputpath)
		else:
			# Original single/multiple repository mode
			if len(args) < 2:
				usage()
				sys.exit(0)

			self.run_single_mode(args)

	def run_multi_repo(self, repositories, base_outputpath):
		"""Generate reports for multiple repositories with enhanced error handling."""
		rundir = os.getcwd()

		# Validate and create base output directory
		try:
			os.makedirs(base_outputpath, exist_ok=True)
		except PermissionError:
			print(f'FATAL: Permission denied creating output directory: {base_outputpath}')
			sys.exit(1)
		except OSError as e:
			print(f'FATAL: Error creating output directory {base_outputpath}: {e}')
			sys.exit(1)

		if not os.path.isdir(base_outputpath):
			print('FATAL: Output path is not a directory or does not exist')
			sys.exit(1)

		# Check write permissions
		if not os.access(base_outputpath, os.W_OK):
			print(f'FATAL: No write permission for output directory: {base_outputpath}')
			sys.exit(1)

		# Using table-based output format (no matplotlib required)
		if conf['verbose']:
			print('Using table-based output format for all visualizations')

		if conf['verbose']:
			print('Multi-repo Configuration:')
			for key, value in conf.items():
				if key.startswith('multi_repo') or key in ['verbose', 'debug', 'processes']:
					print(f'  {key}: {value}')
			print()

		print(f'Base output path: {base_outputpath}')

		successful_reports = 0
		failed_reports = []
		skipped_repos = []
		total_start_time = time.time()

		# Pre-validate all repositories before processing
		print('Pre-validating repositories...')
		validated_repos = []
		for repo_data in repositories:
			if len(repo_data) == 3:
				repo_name, repo_path, repo_type = repo_data
			else:
				# Handle old format for backward compatibility
				repo_name, repo_path = repo_data[:2]
				repo_type = 'regular'

			# Validate repository accessibility
			if not self._validate_repository_access(repo_name, repo_path):
				skipped_repos.append((repo_name, 'Repository validation failed'))
				continue

			validated_repos.append((repo_name, repo_path, repo_type))

		if skipped_repos:
			print(f'Skipping {len(skipped_repos)} invalid repositories:')
			for repo_name, reason in skipped_repos:
				print(f'  - {repo_name}: {reason}')

		if not validated_repos:
			print('FATAL: No valid repositories to process')
			sys.exit(1)

		print(f'Processing {len(validated_repos)} validated repositories...')

		# Determine if we should use parallel processing
		use_parallel = (conf.get('multi_repo_parallel', True) and
						len(validated_repos) > 1 and
						conf.get('multi_repo_max_workers', 4) > 1)

		if use_parallel:
			successful_reports, failed_reports = self._process_repositories_parallel(
				validated_repos, base_outputpath, rundir)
		else:
			# Sequential processing (original approach)
			for i, (repo_name, repo_path, repo_type) in enumerate(validated_repos, 1):
				repo_start_time = time.time()
				print(f'\n{"="*60}')
				print(f'Processing repository {i}/{len(validated_repos)}: {repo_name}')
				print(f'Repository path: {repo_path}')
				print(f'Repository type: {repo_type}')

				# Create repository-specific output directory with safe naming
				safe_repo_name = self._sanitize_filename(repo_name)
				repo_output_path = os.path.join(base_outputpath, f'{safe_repo_name}_report')

				try:
					# Create output directory
					os.makedirs(repo_output_path, exist_ok=True)
					if not os.access(repo_output_path, os.W_OK):
						raise PermissionError(f'No write permission for {repo_output_path}')

					print(f'Report output path: {repo_output_path}')

					# Process this repository with timeout protection
					self._process_single_repository_safe(repo_path, repo_output_path, rundir, repo_name)

					repo_time = time.time() - repo_start_time
					successful_reports += 1
					print(f'✓ Successfully generated report for {repo_name} in {repo_time:.2f}s')

				except KeyboardInterrupt:
					print(f'\n✗ Interrupted while processing {repo_name}')
					failed_reports.append((repo_name, 'Processing interrupted by user'))
					break
				except Exception as e:
					repo_time = time.time() - repo_start_time
					error_msg = str(e)
					failed_reports.append((repo_name, error_msg))
					print(f'✗ Failed to generate report for {repo_name} after {repo_time:.2f}s: {error_msg}')
					if conf['debug']:
						import traceback
						traceback.print_exc()

					# Try to clean up partial output
					try:
						if os.path.exists(repo_output_path):
							import shutil
							shutil.rmtree(repo_output_path)
							if conf['verbose']:
								print(f'  Cleaned up partial output directory: {repo_output_path}')
					except Exception as cleanup_error:
						if conf['debug']:
							print(f'  Warning: Could not clean up {repo_output_path}: {cleanup_error}')

		# Generate summary report
		total_time = time.time() - total_start_time
		self._generate_multi_repo_summary(base_outputpath, validated_repos, successful_reports,
										 failed_reports, skipped_repos, total_time)

		# Final summary
		print(f'\n{"="*60}')
		print(f'Multi-repository report generation complete in {total_time:.2f}s!')
		print(f'Successfully processed: {successful_reports}/{len(validated_repos)} repositories')

		if skipped_repos:
			print(f'Skipped repositories: {len(skipped_repos)}')

		if failed_reports:
			print('\nFailed repositories:')
			for repo_name, error in failed_reports:
				print(f'  - {repo_name}: {error}')

		if successful_reports > 0:
			print(f'\nReports generated in: {base_outputpath}')
			print('Repository reports:')
			for repo_name, repo_path, repo_type in validated_repos:
				safe_repo_name = self._sanitize_filename(repo_name)
				if not any(repo_name == failed[0] for failed in failed_reports):
					report_path = os.path.join(base_outputpath, f'{safe_repo_name}_report')
					print(f'  - {repo_name}: {report_path}/gitstats_metrics.json')

			summary_path = os.path.join(base_outputpath, 'multi_repo_summary.json')
			if os.path.exists(summary_path):
				print(f'\nSummary report: {summary_path}')

	def _validate_repository_access(self, repo_name, repo_path):
		"""Validate that a repository is accessible and can be processed."""
		try:
			# Check basic path validity
			if not os.path.exists(repo_path):
				if conf['verbose']:
					print(f'  Repository path does not exist: {repo_path}')
				return False

			if not os.path.isdir(repo_path):
				if conf['verbose']:
					print(f'  Repository path is not a directory: {repo_path}')
				return False

			# Check read permissions
			if not os.access(repo_path, os.R_OK):
				if conf['verbose']:
					print(f'  No read permission for repository: {repo_path}')
				return False

			# Validate it's a proper git repository
			if not is_git_repository(repo_path):
				if conf['verbose']:
					print(f'  Not a valid git repository: {repo_path}')
				return False

			# Try to access the repository with git
			prev_dir = os.getcwd()
			try:
				os.chdir(repo_path)
				# Try a simple git command to ensure the repo is accessible
				result = getpipeoutput(['git rev-parse --git-dir'], quiet=True)
				if not result.strip():
					if conf['verbose']:
						print(f'  Git repository appears to be corrupted: {repo_path}')
					return False
			except Exception as e:
				if conf['verbose']:
					print(f'  Error accessing git repository {repo_path}: {e}')
				return False
			finally:
				os.chdir(prev_dir)

			return True

		except Exception as e:
			if conf['verbose']:
				print(f'  Error validating repository {repo_name}: {e}')
			return False

	def _sanitize_filename(self, filename):
		"""Sanitize a filename to be safe for filesystem use."""
		import re
		# Replace any characters that might be problematic in filenames
		safe_name = re.sub(r'[<>:"/\\|?*]', '_', filename)
		# Remove any leading/trailing dots or spaces
		safe_name = safe_name.strip('. ')
		# Ensure it's not empty
		if not safe_name:
			safe_name = 'unnamed_repo'
		return safe_name

	def _process_single_repository_safe(self, repo_path, output_path, rundir, repo_name):
		"""Process a single repository with additional safety measures."""
		try:
			self.process_single_repository(repo_path, output_path, rundir)
		except KeyboardInterrupt:
			# Re-raise keyboard interrupt to allow proper cleanup
			raise
		except Exception as e:
			# Enhance error message with repository context
			enhanced_error = f"Error processing {repo_name}: {str(e)}"
			raise Exception(enhanced_error) from e

	def _generate_multi_repo_summary(self, base_outputpath, repositories, successful_count,
									failed_reports, skipped_repos, total_time):
		"""Generate a summary JSON report for multi-repository processing."""
		import json
		try:
			summary_file = os.path.join(base_outputpath, 'multi_repo_summary.json')

			summary_data = {
				'generated_at': datetime.datetime.now().isoformat(),
				'total_processing_time_seconds': total_time,
				'statistics': {
					'total_repositories': len(repositories),
					'successful': successful_count,
					'failed': len(failed_reports),
					'skipped': len(skipped_repos)
				},
				'repositories': []
			}

			# Successful repositories
			for repo_name, repo_path, repo_type in repositories:
				if not any(repo_name == failed[0] for failed in failed_reports):
					safe_repo_name = self._sanitize_filename(repo_name)
					report_file = os.path.join(base_outputpath, f'{safe_repo_name}_report', 'gitstats_metrics.json')
					summary_data['repositories'].append({
						'name': repo_name,
						'path': repo_path,
						'type': repo_type,
						'status': 'success',
						'report_file': report_file
					})

			# Failed repositories
			for repo_name, error in failed_reports:
				summary_data['repositories'].append({
					'name': repo_name,
					'status': 'failed',
					'error': error
				})

			# Skipped repositories
			for repo_name, reason in skipped_repos:
				summary_data['repositories'].append({
					'name': repo_name,
					'status': 'skipped',
					'reason': reason
				})

			with open(summary_file, 'w', encoding='utf-8') as f:
				json.dump(summary_data, f, indent=2, ensure_ascii=False)

			print(json.dumps(summary_data, indent=2, ensure_ascii=False), file=getattr(self, 'original_stdout', sys.stdout))

			if conf['verbose']:
				print(f'Generated summary report: {summary_file}')

		except Exception as e:
			if conf['verbose']:
				print(f'Warning: Could not generate summary report: {e}')

	def run_single_mode(self, args):
		"""Original single/multiple repository mode with subfolder creation."""
		base_outputpath = os.path.abspath(args[-1])
		rundir = os.getcwd()

		# Validate git paths
		git_paths = args[0:-1]
		for gitpath in git_paths:
			if not os.path.exists(gitpath):
				print(f'FATAL: Git repository path does not exist: {gitpath}')
				sys.exit(1)
			if not os.path.isdir(gitpath):
				print(f'FATAL: Git repository path is not a directory: {gitpath}')
				sys.exit(1)
			git_dir = os.path.join(gitpath, '.git')
			if not os.path.exists(git_dir):
				print(f'FATAL: Path is not a git repository (no .git directory found): {gitpath}')
				sys.exit(1)

		# Validate and create base output directory
		try:
			os.makedirs(base_outputpath, exist_ok=True)
		except PermissionError:
			print(f'FATAL: Permission denied creating output directory: {base_outputpath}')
			sys.exit(1)
		except OSError as e:
			print(f'FATAL: Error creating output directory {base_outputpath}: {e}')
			sys.exit(1)

		if not os.path.isdir(base_outputpath):
			print('FATAL: Output path is not a directory or does not exist')
			sys.exit(1)

		# Check write permissions
		if not os.access(base_outputpath, os.W_OK):
			print(f'FATAL: No write permission for output directory: {base_outputpath}')
			sys.exit(1)

		print('Using table-based output format (matplotlib not required)')

		if conf['verbose']:
			print('Configuration:')
			for key, value in conf.items():
				print(f'  {key}: {value}')
			print()

		# Initialize variables for Pylance static analysis
		outputpath = None
		all_repo_metrics = []

		# Process each repository and create subfolders
		for gitpath in git_paths:
			# Get repository name from the path
			repo_name = os.path.basename(os.path.abspath(gitpath))
			safe_repo_name = self._sanitize_filename(repo_name)

			# Create repository-specific output directory
			outputpath = os.path.join(base_outputpath, f'{safe_repo_name}_report')

			# Validate and create specific output directory
			try:
				os.makedirs(outputpath, exist_ok=True)
			except PermissionError:
				print(f'FATAL: Permission denied creating repository output directory: {outputpath}')
				sys.exit(1)
			except OSError as e:
				print(f'FATAL: Error creating repository output directory {outputpath}: {e}')
				sys.exit(1)

			if not os.access(outputpath, os.W_OK):
				print(f'FATAL: No write permission for repository output directory: {outputpath}')
				sys.exit(1)

			print('Git path: %s' % gitpath)
			print('Output path: %s' % outputpath)

			cachefile = os.path.join(outputpath, 'gitstats.cache')

			data = GitDataCollector()
			data.loadCache(cachefile)

			prevdir = os.getcwd()
			os.chdir(gitpath)

			print('Collecting data...')
			data.collect(gitpath)

			# Calculate MI for current repository (if enabled)
			if conf['calculate_mi_per_repository']:
				print('Calculating Maintainability Index (MI) for current repository...')
				mi_results = data.calculate_mi_for_repository(gitpath)
				if mi_results:
					print(f'✓ MI calculation completed for {mi_results.get("files_analyzed", 0)} files')
				else:
					print('⚠️  MI calculation failed or no files found')

				print('')
				mccabe_results = data.calculate_mccabe_for_repository(gitpath)
				if mccabe_results:
					print(f'✓ McCabe calculation completed for {mccabe_results.get("files_analyzed", 0)} files')
				else:
					print('⚠️  McCabe calculation failed or no files found')

				print('')
				halstead_results = data.calculate_halstead_for_repository(gitpath)
				if halstead_results:
					print(f'✓ Halstead calculation completed for {halstead_results.get("files_analyzed", 0)} files')
				else:
					print('⚠️  Halstead calculation failed or no files found')

				print('')
				oop_results = data.calculate_oop_for_repository(gitpath)
				if oop_results:
					print(f'✓ OOP calculation completed for {oop_results.get("files_analyzed", 0)} files')
				else:
					print('⚠️  OOP calculation failed or no files found')
			else:
				print('Skipping per-repository metrics calculation (disabled in configuration)')

			os.chdir(prevdir)

			print('Refining data...')
			data.saveCache(cachefile)
			data.refine()

			os.chdir(rundir)

			# Hotspot analysis
			hotspot_data = {}
			if conf.get('analyze_hotspots', True):
				print('Analyzing code hotspots...')
				try:
					detector = HotspotDetector(data)
					hotspots = detector.analyze()
					hotspot_data = {
						'hotspots': hotspots,
						'summary': detector.get_summary()
					}
					critical_count = len([h for h in hotspots if h.get('risk_level') == 'critical'])
					high_count = len([h for h in hotspots if h.get('risk_level') == 'high'])
					print(f'✓ Hotspot analysis completed: {critical_count} critical, {high_count} high-risk files')
				except Exception as e:
					if conf.get('debug'):
						import traceback
						traceback.print_exc()
					print(f'⚠️  Hotspot analysis failed: {e}')

			print('Generating JSON report...')
			try:
				exporter = MetricsExporter(data, hotspot_data)
				json_path = exporter.export_json(outputpath)
				all_repo_metrics.append(exporter.get_metrics_dict())
				print(f'✓ JSON report saved to: {json_path}')
			except Exception as e:
				print(f'⚠️  JSON export failed: {e}')

			print(f'✓ Successfully generated report for {repo_name}')


		time_end = time.time()
		exectime_internal = time_end - time_start
		exectime_external = get_exectime_external()
		external_percentage = (100.0 * exectime_external) / exectime_internal if exectime_internal > 0 else 0.0
		print('Execution time %.5f secs, %.5f secs (%.2f %%) in external commands)' % (exectime_internal, exectime_external, external_percentage))

		import json
		if len(all_repo_metrics) == 1:
			print(json.dumps(all_repo_metrics[0], cls=DateTimeEncoder, indent=2), file=getattr(self, 'original_stdout', sys.stdout))
		elif len(all_repo_metrics) > 1:
			print(json.dumps(all_repo_metrics, cls=DateTimeEncoder, indent=2), file=getattr(self, 'original_stdout', sys.stdout))

		if len(git_paths) == 1:
			# For single repository, show the direct path to the report
			repo_name = os.path.basename(os.path.abspath(git_paths[0]))
			safe_repo_name = self._sanitize_filename(repo_name)
			outputpath = os.path.join(base_outputpath, f'{safe_repo_name}_report')

			if sys.stdin.isatty():
				print('Report has been generated at:')
				print(f'  {os.path.join(outputpath, "gitstats_metrics.json")}')
				print()
		else:
			# For multiple repositories, show the base path
			if sys.stdin.isatty():
				print('Reports have been generated in subfolders under:')
				print(f'  {base_outputpath}')
				print()

	def _process_repositories_parallel(self, repositories, base_outputpath, rundir):
		"""Process repositories in parallel for improved performance."""
		# Configuration
		max_workers = conf.get('multi_repo_max_workers', 4)
		batch_size = conf.get('multi_repo_batch_size', 10)
		progress_interval = conf.get('multi_repo_progress_interval', 5)

		print(f'Using parallel processing with {max_workers} workers, batch size: {batch_size}')

		# Process repositories in batches to manage memory
		total_repos = len(repositories)

		# Shared state for progress tracking
		progress_state: Dict[str, Any] = {
			'processed_count': 0,
			'last_progress_time': time.time(),
			'start_time': time.time(),
			'completion_times': [],
			'successful_reports': 0,
			'failed_reports': []
		}

		# Create a thread-safe progress tracker
		progress_lock = threading.Lock()

		def _process_repository_worker(repo_data):
			"""Worker function for processing a single repository."""
			repo_name, repo_path, repo_type = repo_data

			try:
				# Create output directory with safe naming
				safe_repo_name = self._sanitize_filename(repo_name)
				repo_output_path = os.path.join(base_outputpath, f'{safe_repo_name}_report')

				# Create directory
				os.makedirs(repo_output_path, exist_ok=True)
				if not os.access(repo_output_path, os.W_OK):
					raise PermissionError(f'No write permission for {repo_output_path}')

				# Process the repository
				repo_start_time = time.time()
				self._process_single_repository_safe(repo_path, repo_output_path, rundir, repo_name)
				repo_time = time.time() - repo_start_time

				# Thread-safe progress update with ETA
				with progress_lock:
					progress_state['processed_count'] += 1
					current_time = time.time()
					progress_state['completion_times'].append(current_time)

					if (current_time - progress_state['last_progress_time'] >= progress_interval or
						progress_state['processed_count'] == total_repos):

						# Calculate ETA
						if len(progress_state['completion_times']) >= 5:
							avg_time_per_repo = (current_time - progress_state['start_time']) / progress_state['processed_count']
							remaining_repos = total_repos - progress_state['processed_count']
							eta_seconds = remaining_repos * avg_time_per_repo
							eta_str = f', ETA: {int(eta_seconds//60)}m {int(eta_seconds%60)}s' if eta_seconds > 0 else ''
						else:
							eta_str = ''

						print(f'Progress: {progress_state["processed_count"]}/{total_repos} repositories completed '
							  f'({(progress_state["processed_count"]/total_repos)*100:.1f}%){eta_str}')
						progress_state['last_progress_time'] = current_time

				return (repo_name, 'success', repo_time, None)

			except Exception as e:
				error_msg = str(e)

				# Clean up partial output on error
				if conf.get('multi_repo_cleanup_on_error', True):
					try:
						safe_repo_name = self._sanitize_filename(repo_name)
						repo_output_path = os.path.join(base_outputpath, f'{safe_repo_name}_report')
						if os.path.exists(repo_output_path):
							import shutil
							shutil.rmtree(repo_output_path)
					except Exception:
						pass  # Ignore cleanup errors

				with progress_lock:
					progress_state['processed_count'] += 1

				return (repo_name, 'failed', 0, error_msg)

		# Process repositories in batches
		for batch_start in range(0, total_repos, batch_size):
			batch_end = min(batch_start + batch_size, total_repos)
			batch_repos = repositories[batch_start:batch_end]

			if conf['verbose']:
				print(f'\nProcessing batch {batch_start//batch_size + 1}/{(total_repos + batch_size - 1)//batch_size}: '
					  f'repositories {batch_start + 1}-{batch_end}')

			# Use ThreadPoolExecutor for I/O-bound repository processing
			# (ProcessPoolExecutor would be better for CPU-bound, but git operations are mostly I/O)
			futures = {}
			with ThreadPoolExecutor(max_workers=max_workers) as executor:
				try:
					# Submit all jobs in the batch
					futures = {executor.submit(_process_repository_worker, repo_data): repo_data
							  for repo_data in batch_repos}

					# Collect results as they complete
					for future in as_completed(futures):
						repo_name, status, duration, error = future.result()

						if status == 'success':
							progress_state['successful_reports'] += 1
							if conf['verbose']:
								print(f'✓ {repo_name} completed in {duration:.2f}s')
						else:
							progress_state['failed_reports'].append((repo_name, error))
							if conf['verbose']:
								print(f'✗ {repo_name} failed: {error}')

				except KeyboardInterrupt:
					print('\nProcessing interrupted by user')
					# Cancel remaining futures
					for future in futures:
						future.cancel()
					break

			# Memory cleanup between batches
			gc.collect()

		return progress_state['successful_reports'], progress_state['failed_reports']

	def process_single_repository(self, repo_path, output_path, rundir):
		"""Process a single repository and generate its report with improved resource management."""

		# Validate inputs
		if not os.path.exists(repo_path):
			raise FileNotFoundError(f"Repository path does not exist: {repo_path}")
		if not os.path.isdir(repo_path):
			raise NotADirectoryError(f"Repository path is not a directory: {repo_path}")
		if not is_git_repository(repo_path):
			raise ValueError(f"Path is not a valid git repository: {repo_path}")

		cachefile = os.path.join(output_path, 'gitstats.cache')

		# Initialize data collector with proper cleanup
		data = None
		try:
			data = GitDataCollector()

			# Load cache if available
			try:
				data.loadCache(cachefile)
				if conf['verbose']:
					print(f'  Loaded cache from: {cachefile}')
			except Exception as e:
				if conf['verbose']:
					print(f'  Could not load cache: {e}')

			if conf['verbose']:
				print(f'  Collecting data from: {repo_path}')

			# Change to repository directory
			prevdir = os.getcwd()
			try:
				os.chdir(repo_path)

				# Collect data with memory monitoring
				initial_memory = self._get_memory_usage()

				data.collect(repo_path)

				# Calculate comprehensive metrics for current repository (if enabled)
				if conf['calculate_mi_per_repository']:
					print('  Calculating Maintainability Index (MI)...')
					mi_results = data.calculate_mi_for_repository(repo_path)
					if mi_results:
						files_analyzed = mi_results.get("files_analyzed", 0)
						print(f'  ✓ MI calculation completed for {files_analyzed} files')
						if files_analyzed > 0:
							avg_mi = mi_results.get("summary", {}).get("average_mi", 0)
							print(f'  📊 Average MI: {avg_mi:.1f}')
					else:
						print('  ⚠️  MI calculation failed or no files found')

					print('  Calculating McCabe Complexity...')
					mccabe_results = data.calculate_mccabe_for_repository(repo_path)
					if mccabe_results:
						files_analyzed = mccabe_results.get("files_analyzed", 0)
						print(f'  ✓ McCabe calculation completed for {files_analyzed} files')
						if files_analyzed > 0:
							avg_complexity = mccabe_results.get("summary", {}).get("average_complexity", 0)
							print(f'  📊 Average Complexity: {avg_complexity:.1f}')
					else:
						print('  ⚠️  McCabe calculation failed or no files found')

					print('  Calculating Halstead Metrics...')
					halstead_results = data.calculate_halstead_for_repository(repo_path)
					if halstead_results:
						files_analyzed = halstead_results.get("files_analyzed", 0)
						print(f'  ✓ Halstead calculation completed for {files_analyzed} files')
						if files_analyzed > 0:
							avg_effort = halstead_results.get("summary", {}).get("average_effort", 0)
							print(f'  📊 Average Effort: {avg_effort:.1f}')
					else:
						print('  ⚠️  Halstead calculation failed or no files found')

					print('  Calculating OOP Metrics...')
					oop_results = data.calculate_oop_for_repository(repo_path)
					if oop_results:
						files_analyzed = oop_results.get("files_analyzed", 0)
						print(f'  ✓ OOP calculation completed for {files_analyzed} files')
						if files_analyzed > 0:
							files_with_oop = oop_results.get("summary", {}).get("files_with_oop", 0)
							print(f'  📊 Files with OOP: {files_with_oop}/{files_analyzed}')
					else:
						print('  ⚠️  OOP calculation failed or no files found')
				else:
					if conf['verbose']:
						print('  Skipping comprehensive metrics calculation (disabled in configuration)')

				final_memory = self._get_memory_usage()
				if conf['verbose'] and initial_memory and final_memory:
					memory_delta = final_memory - initial_memory
					print(f'  Memory usage: {memory_delta:.1f} MB')

			finally:
				os.chdir(prevdir)

			if conf['verbose']:
				print('  Refining data...')

			# Save cache before refining (in case refining fails)
			try:
				data.saveCache(cachefile)
				if conf['verbose']:
					print(f'  Saved cache to: {cachefile}')
			except Exception as e:
				if conf['verbose']:
					print(f'  Warning: Could not save cache: {e}')

			data.refine()

			# Return to original directory
			os.chdir(rundir)

			if conf['verbose']:
				print('  Generating JSON report...')

			try:
				hotspot_data = {}
				if conf.get('analyze_hotspots', True):
					try:
						detector = HotspotDetector(data)
						hotspots = detector.analyze()
						hotspot_data = {
							'hotspots': hotspots,
							'summary': detector.get_summary()
						}
					except Exception as e:
						if conf.get('verbose'):
							print(f'  Warning: Hotspot analysis failed: {e}')

				json_path = export_to_json(data, output_path, hotspot_data)
				if conf['verbose']:
					print(f'  ✓ JSON report saved to: {json_path}')
			except Exception as e:
				print(f'  Warning: JSON report generation failed: {e}')
				if conf['debug']:
					import traceback
					traceback.print_exc()

				# Clean up partial output on error if configured to do so
				if conf.get('multi_repo_cleanup_on_error', True):
					try:
						if os.path.exists(output_path):
							import shutil
							# Only clean up if it looks like our output directory
							if output_path.endswith('_report'):
								shutil.rmtree(output_path)
								if conf['verbose']:
									print(f'  Cleaned up partial output: {output_path}')
					except Exception as cleanup_error:
						if conf['debug']:
							print(f'  Warning: Cleanup failed: {cleanup_error}')
				raise

		finally:
			# Force garbage collection to free memory
			if data:
				del data
			gc.collect()

	def _get_memory_usage(self):
		"""Get current memory usage in MB. Returns None if unavailable."""
		try:
			import psutil  # type: ignore
			process = psutil.Process()
			return process.memory_info().rss / 1024 / 1024  # Convert to MB
		except ImportError:
			# Fallback to basic memory info on systems without psutil
			try:
				import resource
				return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Convert to MB (Linux)
			except (ImportError, AttributeError):
				return None
		except Exception:
			return None

	def _check_memory_pressure(self, max_memory_mb=2048):
		"""Check if we're using too much memory and suggest cleanup."""
		current_memory = self._get_memory_usage()
		if current_memory and current_memory > max_memory_mb:
			if conf['verbose']:
				print(f'Warning: High memory usage detected ({current_memory:.1f} MB). '
					  f'Consider reducing batch size or max workers.')
			return True
		return False

def main():
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

if __name__=='__main__':
	main()
