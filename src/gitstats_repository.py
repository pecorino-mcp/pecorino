"""
Repository discovery module for Gitstats3.

Contains functions for discovering and validating git repositories.
"""

import os
import fnmatch
import subprocess
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

from .gitstats_config import conf, get_config
from .gitstats_gitcommands import is_git_repository


def _is_bare_repository(path):
	"""Check if the directory is a bare git repository."""
	try:
		# Bare repositories have objects and refs directories directly
		objects_dir = os.path.join(path, 'objects')
		refs_dir = os.path.join(path, 'refs')
		head_file = os.path.join(path, 'HEAD')
		
		if not (os.path.exists(objects_dir) and os.path.exists(refs_dir) and os.path.exists(head_file)):
			return False
		
		# Additional check: try to run git command to confirm it's a bare repo
		try:
			result = getpipeoutput([f'cd "{path}" && git rev-parse --is-bare-repository'], quiet=True)
			return result.strip().lower() == 'true'
		except:
			return False
			
	except (OSError, PermissionError):
		return False
	
	return True

def discover_repositories(scan_path, recursive=False, max_depth=10, include_patterns=None, exclude_patterns=None):
	"""Discover all git repositories in a directory with advanced options and concurrent scanning.
	
	Args:
		scan_path: Directory to scan for repositories
		recursive: If True, scan subdirectories recursively
		max_depth: Maximum depth for recursive scanning (default: 3)
		include_patterns: List of glob patterns for directories to include
		exclude_patterns: List of glob patterns for directories to exclude
	
	Returns:
		List of tuples: (repo_name, repo_path, repo_type)
		where repo_type is 'regular', 'bare', or 'worktree'
	"""
	repositories = []
	
	if not os.path.exists(scan_path):
		print(f'Warning: Scan path does not exist: {scan_path}')
		return repositories
	
	if not os.path.isdir(scan_path):
		print(f'Warning: Scan path is not a directory: {scan_path}')
		return repositories
	
	# Set default patterns if not provided
	if exclude_patterns is None:
		exclude_patterns = [
			'.*',  # Hidden directories
			'node_modules', 
			'venv', 
			'__pycache__',
			'build',
			'dist',
			'target',  # Maven/Gradle build dirs
			'bin',
			'obj'      # .NET build dirs
		]
	
	# Use fast concurrent scanning if enabled
	if conf.get('multi_repo_fast_scan', True) and recursive:
		return _discover_repositories_concurrent(scan_path, max_depth, include_patterns, exclude_patterns)
	
	# Fallback to original sequential scanning
	
	def _should_exclude_directory(dir_name, dir_path):
		"""Check if directory should be excluded based on patterns."""
		import fnmatch
		
		# Check exclude patterns
		for pattern in exclude_patterns:
			if fnmatch.fnmatch(dir_name, pattern):
				return True
		
		# Check include patterns (if specified, directory must match at least one)
		if include_patterns:
			for pattern in include_patterns:
				if fnmatch.fnmatch(dir_name, pattern):
					return False
			return True  # No include pattern matched
		
		return False
	
	def _determine_repo_type(repo_path):
		"""Determine the type of git repository."""
		git_dir = os.path.join(repo_path, '.git')
		
		if os.path.isdir(git_dir):
			return 'regular'
		elif os.path.isfile(git_dir):
			return 'worktree'
		elif _is_bare_repository(repo_path):
			return 'bare'
		else:
			return 'unknown'
	
	def _scan_directory(current_path, current_depth=0):
		"""Recursively scan directory for git repositories."""
		if current_depth > max_depth:
			return
		
		try:
			# Get list of items, handle permission errors gracefully
			try:
				items = os.listdir(current_path)
			except PermissionError:
				if conf['verbose']:
					print(f'  Permission denied accessing: {current_path}')
				return
			except OSError as e:
				if conf['verbose']:
					print(f'  Error accessing {current_path}: {e}')
				return
			
			# Check if current directory is a git repository
			if is_git_repository(current_path):
				repo_name = os.path.basename(current_path)
				repo_type = _determine_repo_type(current_path)
				repositories.append((repo_name, current_path, repo_type))
				
				if conf['verbose']:
					print(f'  Found {repo_type} repository: {repo_name} at {current_path}')
				
				# Don't scan inside git repositories to avoid nested repos
				return
			
			# If recursive scanning is enabled, scan subdirectories
			if recursive:
				for item in sorted(items):  # Sort for consistent ordering
					item_path = os.path.join(current_path, item)
					
					# Skip if not a directory or if it's a symbolic link to avoid loops
					if not os.path.isdir(item_path):
						continue
					
					# Handle symbolic links carefully to avoid infinite loops
					if os.path.islink(item_path):
						try:
							# Resolve the link and check if it points outside scan_path
							real_item_path = os.path.realpath(item_path)
							scan_real_path = os.path.realpath(scan_path)
							
							# Skip if symlink points outside the scan directory
							if not real_item_path.startswith(scan_real_path):
								if conf['debug']:
									print(f'  Skipping symlink pointing outside scan path: {item_path}')
								continue
								
							# Skip if we've already seen this real path (circular symlinks)
							if real_item_path in seen_paths:
								if conf['debug']:
									print(f'  Skipping circular symlink: {item_path}')
								continue
							seen_paths.add(real_item_path)
							
						except (OSError, ValueError):
							if conf['debug']:
								print(f'  Skipping invalid symlink: {item_path}')
							continue
					
					# Check exclusion patterns
					if _should_exclude_directory(item, item_path):
						if conf['debug']:
							print(f'  Excluding directory: {item_path}')
						continue
					
					# Recursively scan subdirectory
					_scan_directory(item_path, current_depth + 1)
			
		except Exception as e:
			if conf['verbose']:
				print(f'  Error scanning {current_path}: {e}')
	
	# Keep track of seen paths to handle symbolic links
	seen_paths = set()
	seen_paths.add(os.path.realpath(scan_path))
	
	if conf['verbose']:
		print(f'Scanning for repositories in: {scan_path}')
		print(f'  Recursive: {recursive}')
		print(f'  Max depth: {max_depth}')
		if include_patterns:
			print(f'  Include patterns: {include_patterns}')
		if exclude_patterns:
			print(f'  Exclude patterns: {exclude_patterns}')
	
	# Start scanning
	_scan_directory(scan_path)
	
	if conf['verbose']:
		print(f'Repository discovery complete. Found {len(repositories)} repositories.')
	
	return repositories

def _discover_repositories_concurrent(scan_path, max_depth=10, include_patterns=None, exclude_patterns=None):
	"""Fast concurrent repository discovery using ThreadPoolExecutor."""
	repositories = []
	repositories_lock = threading.Lock()
	
	# Set default patterns
	if exclude_patterns is None:
		exclude_patterns = [
			'.*', 'node_modules', 'venv', '__pycache__',
			'build', 'dist', 'target', 'bin', 'obj'
		]
	
	def _should_exclude_directory(dir_name, dir_path):
		"""Check if directory should be excluded based on patterns."""
		import fnmatch
		
		for pattern in exclude_patterns:
			if fnmatch.fnmatch(dir_name, pattern):
				return True
		
		if include_patterns:
			for pattern in include_patterns:
				if fnmatch.fnmatch(dir_name, pattern):
					return False
			return True
		
		return False
	
	def _determine_repo_type(repo_path):
		"""Determine the type of git repository."""
		git_dir = os.path.join(repo_path, '.git')
		
		if os.path.isdir(git_dir):
			return 'regular'
		elif os.path.isfile(git_dir):
			return 'worktree'
		elif _is_bare_repository(repo_path):
			return 'bare'
		else:
			return 'unknown'
	
	def _scan_directory_concurrent(path_depth_tuple):
		"""Thread-safe directory scanning function."""
		current_path, current_depth = path_depth_tuple
		
		if current_depth > max_depth:
			return []
		
		local_repos = []
		
		try:
			# Handle permission errors gracefully
			try:
				items = os.listdir(current_path)
			except (PermissionError, OSError) as e:
				if conf['verbose']:
					print(f'  Permission/access error: {current_path}: {e}')
				return []
			
			# Check if current directory is a git repository
			if is_git_repository(current_path):
				repo_name = os.path.basename(current_path)
				repo_type = _determine_repo_type(current_path)
				local_repos.append((repo_name, current_path, repo_type))
				
				if conf['verbose']:
					print(f'  Found {repo_type} repository: {repo_name}')
				
				# Don't scan inside git repositories
				return local_repos
			
			# Collect subdirectories to scan
			subdirs_to_scan = []
			for item in sorted(items):
				item_path = os.path.join(current_path, item)
				
				if not os.path.isdir(item_path):
					continue
				
				# Handle symlinks carefully
				if os.path.islink(item_path):
					try:
						real_path = os.path.realpath(item_path)
						scan_real_path = os.path.realpath(scan_path)
						
						if not real_path.startswith(scan_real_path):
							continue
					except (OSError, ValueError):
						continue
				
				# Check exclusion patterns
				if _should_exclude_directory(item, item_path):
					if conf['debug']:
						print(f'  Excluding: {item_path}')
					continue
				
				subdirs_to_scan.append((item_path, current_depth + 1))
			
			# Recursively scan subdirectories (will be handled by thread pool)
			return subdirs_to_scan
			
		except Exception as e:
			if conf['verbose']:
				print(f'  Error scanning {current_path}: {e}')
			return []
	
	if conf['verbose']:
		print(f'Starting concurrent repository discovery in: {scan_path}')
		print(f'  Max depth: {max_depth}')
		print(f'  Max workers: {min(conf["multi_repo_max_workers"], 8)}')
	
	# Use ThreadPoolExecutor for I/O bound directory scanning
	max_workers = min(conf.get('multi_repo_max_workers', 4), 8)  # Cap at 8 threads
	
	# Queue of directories to scan
	dirs_to_scan = queue.Queue()
	dirs_to_scan.put((scan_path, 0))
	
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		# Keep track of active futures
		active_futures = set()
		
		while not dirs_to_scan.empty() or active_futures:
			# Submit new tasks if we have directories to scan and available workers
			while not dirs_to_scan.empty() and len(active_futures) < max_workers:
				path_depth = dirs_to_scan.get()
				future = executor.submit(_scan_directory_concurrent, path_depth)
				active_futures.add(future)
			
			# Check completed futures
			completed_futures = set()
			for future in active_futures:
				if future.done():
					completed_futures.add(future)
					try:
						result = future.result()
						
						# Result can be either repositories or subdirectories to scan
						for item in result:
							if len(item) == 3:  # It's a repository (name, path, type)
								with repositories_lock:
									repositories.append(item)
							elif len(item) == 2:  # It's a directory to scan (path, depth)
								dirs_to_scan.put(item)
					except Exception as e:
						if conf['verbose']:
							print(f'  Error in scanning task: {e}')
			
			# Remove completed futures
			active_futures -= completed_futures
			
			# Small delay to prevent busy waiting
			if active_futures:
				import time
				time.sleep(0.001)
	
	if conf['verbose']:
		print(f'Concurrent repository discovery complete. Found {len(repositories)} repositories.')
	
	return repositories

