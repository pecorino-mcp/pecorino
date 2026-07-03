"""
Git data collector module for Pecorino.

Contains the GitDataCollector class that extends DataCollector with git-specific data collection.
"""

import datetime
import gc
import os
import logging

logger = logging.getLogger(__name__)

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from src.metrics.analyzers import analyzesloc, getnumoffilesfromrev, getnumoflinesinblob
from src.core.config import conf
from src.core.datacollector import DataCollector
from src.git.commands import (
	get_default_branch,
	get_first_parent_flag,
	getcommitrange,
	getlogrange,
	getpipeoutput,
)
from src.utils.helpers import (
	getkeyssortedbyvaluekey,
	getstatsummarycounts,
	should_include_file,
)


class GitDataCollector(DataCollector):
	def collect(self, dir):
		DataCollector.collect(self, dir)

		# Store the repository path for later use in comprehensive metrics
		self.repository_path = dir

		# Print information about branch scanning
		if conf['scan_default_branch_only']:
			default_branch = get_default_branch()
			logger.info(f'Branch scanning: ONLY scanning default branch ({default_branch})')
		else:
			logger.info('Branch scanning: Scanning ALL branches (default behavior)')

		# Print information about extension filtering
		if conf['filter_by_extensions']:
			logger.info('File extension filtering is ENABLED. Only analyzing files with these extensions:')
			extensions_list = sorted(list(conf['allowed_extensions']))
			logger.info(f'  {", ".join(extensions_list)}')
		else:
			logger.info('File extension filtering is DISABLED. Analyzing all file types.')

		first_parent_flag = get_first_parent_flag()
		self.total_authors += int(getpipeoutput(['git shortlog -s %s %s' % (first_parent_flag, getlogrange()), 'wc -l']))
		#self.total_lines = int(getoutput('git-ls-files -z |xargs -0 cat |wc -l'))

		# Clear tags for each repository to avoid multirepo contamination
		if not hasattr(self, '_first_repo'):
			self._first_repo = True
		else:
			# For subsequent repos, clear tags to avoid mixing
			self.tags = {}

		# tags
		lines = getpipeoutput(['git show-ref --tags']).split('\n')
		for line in lines:
			line = line.strip()
			if not line:
				continue
			parts = line.split(' ', 1)
			if len(parts) < 2:
				continue
			hash, tag = parts[0], parts[1]

			tag = tag.replace('refs/tags/', '')
			output = getpipeoutput(['git log "%s" --pretty=format:"%%at %%aN" -n 1' % hash])
			if len(output) > 0:
				parts = output.split(' ', 1)
				stamp = 0
				try:
					stamp = int(parts[0])
				except (ValueError, IndexError):
					stamp = 0
				self.tags[tag] = { 'stamp': stamp, 'hash' : hash, 'date' : datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d'), 'commits': 0, 'authors': {} }

		# collect info on tags, starting from latest
		tags_sorted_by_date_desc = list(map(lambda el : el[1], reversed(sorted(map(lambda el : (el[1]['date'], el[0]), self.tags.items())))))
		prev = None
		for tag in reversed(tags_sorted_by_date_desc):
			cmd = 'git shortlog -s "%s"' % tag
			if prev != None:
				cmd += ' "^%s"' % prev
			output = getpipeoutput([cmd])
			if len(output) == 0:
				continue
			prev = tag
			for line in output.split('\n'):
				line = line.strip()
				if not line:
					continue
				parts = re.split(r'\s+', line, maxsplit=1)
				if len(parts) < 2:
					continue
				try:
					commits = int(parts[0])
					author = parts[1]
					self.tags[tag]['commits'] += commits
					self.tags[tag]['authors'][author] = commits
				except ValueError:
					continue

		# Collect revision statistics
		# Outputs "<stamp> <date> <time> <timezone> <author> '<' <mail> '>'"
		first_parent_flag = get_first_parent_flag()
		lines = getpipeoutput(['git rev-list %s --pretty=format:"%%at %%ai %%aN <%%aE>" %s' % (first_parent_flag, getlogrange('HEAD')), 'grep -v ^commit']).split('\n')
		for line in lines:
			line = line.strip()
			if not line:
				continue
			parts = line.split(' ', 4)
			if len(parts) < 5:
				continue
			author = ''
			try:
				stamp = int(parts[0])
			except ValueError:
				stamp = 0
			timezone = parts[3]
			if '<' not in parts[4]:
				continue
			author, mail = parts[4].split('<', 1)
			author = author.rstrip()
			mail = mail.rstrip('>')
			domain = '?'
			if mail.find('@') != -1:
				domain = mail.rsplit('@', 1)[1]
			date = datetime.datetime.fromtimestamp(float(stamp))

			# First and last commit stamp (may be in any order because of cherry-picking and patches)
			if stamp > self.repository_stats['last_commit_stamp']:
				self.repository_stats['last_commit_stamp'] = stamp
			if self.repository_stats['first_commit_stamp'] == 0 or stamp < self.repository_stats['first_commit_stamp']:
				self.repository_stats['first_commit_stamp'] = stamp

			# activity
			# hour
			hour = date.hour
			self.activity_metrics['by_hour_of_day'][hour] += 1
			# most active hour?
			if self.activity_metrics['by_hour_of_day'][hour] > self.activity_metrics['hour_of_day_busiest']:
				self.activity_metrics['hour_of_day_busiest'] = self.activity_metrics['by_hour_of_day'][hour]

			# day of week
			day = date.weekday()
			self.activity_metrics['by_day_of_week'][day] += 1

			# domain stats
			if domain not in self.domains:
				self.domains[domain] = defaultdict(int)
			# commits
			self.domains[domain]['commits'] += 1

			# hour of week
			self.activity_metrics['by_hour_of_week'][day][hour] += 1
			# most active hour?
			if self.activity_metrics['by_hour_of_week'][day][hour] > self.activity_metrics['hour_of_week_busiest']:
				self.activity_metrics['hour_of_week_busiest'] = self.activity_metrics['by_hour_of_week'][day][hour]

			# month of year
			month = date.month
			self.activity_metrics['by_month_of_year'][month] += 1

			# yearly/weekly activity
			yyw = date.strftime('%Y-%W')
			self.activity_metrics['by_year_week'][yyw] += 1
			if self.activity_metrics['year_week_peak'] < self.activity_metrics['by_year_week'][yyw]:
				self.activity_metrics['year_week_peak'] = self.activity_metrics['by_year_week'][yyw]

			# author stats
			if author not in self.authors:
				self.authors[author] = { 'lines_added' : 0, 'lines_removed' : 0, 'commits' : 0}
			# commits, note again that commits may be in any date order because of cherry-picking and patches
			if 'last_commit_stamp' not in self.authors[author]:
				self.authors[author]['last_commit_stamp'] = stamp
			if stamp > self.authors[author]['last_commit_stamp']:
				self.authors[author]['last_commit_stamp'] = stamp
			if 'first_commit_stamp' not in self.authors[author]:
				self.authors[author]['first_commit_stamp'] = stamp
			if stamp < self.authors[author]['first_commit_stamp']:
				self.authors[author]['first_commit_stamp'] = stamp

			# author of the month/year
			yymm = date.strftime('%Y-%m')
			self.temporal_data['author_of_month'][yymm][author] += 1
			self.temporal_data['commits_by_month'][yymm] += 1

			yy = date.year
			self.temporal_data['author_of_year'][yy][author] += 1
			self.temporal_data['commits_by_year'][yy] += 1

			# authors: active days
			yymmdd = date.strftime('%Y-%m-%d')
			if 'last_active_day' not in self.authors[author]:
				self.authors[author]['last_active_day'] = yymmdd
				self.authors[author]['active_days'] = set([yymmdd])
			elif yymmdd != self.authors[author]['last_active_day']:
				self.authors[author]['last_active_day'] = yymmdd
				self.authors[author]['active_days'].add(yymmdd)

			# project: active days
			if yymmdd != self.repository_stats['last_active_day']:
				self.repository_stats['last_active_day'] = yymmdd
				self.repository_stats['active_days'].add(yymmdd)

			# timezone
			self.commits_by_timezone[timezone] += 1

		# outputs "<stamp> <files>" for each revision
		first_parent_flag = get_first_parent_flag()
		revlines = getpipeoutput(['git rev-list %s --pretty=format:"%%at %%T" %s' % (first_parent_flag, getlogrange('HEAD')), 'grep -v ^commit']).strip().split('\n')
		lines = []
		revs_to_read = []
		time_rev_count = []
		#Look up rev in cache and take info from cache if found
		#If not append rev to list of rev to read from repo
		for revline in revlines:
			time, rev = revline.split(' ')
			#if cache empty then add time and rev to list of new rev's
			#otherwise try to read needed info from cache
			if 'files_in_tree' not in self.cache:
				revs_to_read.append((time,rev))
				continue
			if rev in self.cache['files_in_tree']:
				lines.append('%d %d' % (int(time), self.cache['files_in_tree'][rev]))
			else:
				revs_to_read.append((time,rev))

		#Read revisions from repo using chunked processing for low memory usage
		if revs_to_read:
			worker_count = min(len(revs_to_read), conf['processes'])
			chunk_size = conf.get('chunk_size', 500)
			if conf['verbose']:
				logger.info(f'Processing {len(revs_to_read)} revisions with {worker_count} workers (chunk size: {chunk_size})')

			# Process in chunks to reduce peak memory usage
			for i in range(0, len(revs_to_read), chunk_size):
				chunk = revs_to_read[i:i + chunk_size]
				with ThreadPoolExecutor(max_workers=worker_count) as executor:
					chunk_results = list(executor.map(getnumoffilesfromrev, chunk))
				time_rev_count.extend(chunk_results)

				# Force cleanup between chunks to reduce memory pressure
				del chunk_results
				self._gc_if_needed()
		else:
			time_rev_count = []

		#Update cache with new revisions and append then to general list
		for (time, rev, count) in time_rev_count:
			if 'files_in_tree' not in self.cache:
				self.cache['files_in_tree'] = {}
			self.cache['files_in_tree'][rev] = count
			lines.append('%d %d' % (int(time), count))

		self.repository_stats['total_commits'] += len(lines)
		for line in lines:
			parts = line.split(' ')
			if len(parts) != 2:
				continue
			(stamp, files) = parts[0:2]
			try:
				timestamp = int(stamp)
				file_count = int(files)
				self.temporal_data['files_by_stamp'][timestamp] = file_count

				# Track files by year (use max file count per year)
				date = datetime.datetime.fromtimestamp(timestamp)
				year = date.year
				if year not in self.temporal_data['files_by_year'] or file_count > self.temporal_data['files_by_year'][year]:
					self.temporal_data['files_by_year'][year] = file_count
			except ValueError:
				logger.info('Warning: failed to parse line "%s"' % line)

		# extensions and size of files
		lines = getpipeoutput(['git ls-tree -r -l -z %s' % getcommitrange('HEAD', end_only = True)]).split('\000')
		blobs_to_read = []
		all_blobs_for_sloc = []  # All blobs for SLOC analysis, regardless of cache status
		for line in lines:
			if len(line) == 0:
				continue
			parts = re.split(r'\s+', line, maxsplit=4)
			if parts[0] == '160000' and parts[3] == '-':
				# skip submodules
				continue
			blob_id = parts[2]
			size = int(parts[3])
			fullpath = parts[4]

			filename = fullpath.split('/')[-1] # strip directories

			# Apply extension filtering - skip files that don't match allowed extensions
			if not should_include_file(filename):
				if conf['verbose'] or conf['debug']:
					logger.info(f'Skipping file (extension not in allowed list): {fullpath}')
				continue

			self.repository_stats['total_size'] += size
			self.repository_stats['total_files'] += 1

			# Track individual file sizes
			self.code_analysis['file_sizes'][fullpath] = size

			if filename.find('.') == -1 or filename.rfind('.') == 0:
				ext = ''
			else:
				ext = filename[(filename.rfind('.') + 1):]
			if len(ext) > conf['max_ext_length']:
				ext = ''
			if ext not in self.code_analysis['extensions']:
				self.code_analysis['extensions'][ext] = {'files': 0, 'lines': 0}
			self.code_analysis['extensions'][ext]['files'] += 1

			# Add all blobs to SLOC analysis list (regardless of cache status)
			all_blobs_for_sloc.append((ext, blob_id))

			#if cache empty then add ext and blob id to list of new blob's
			#otherwise try to read needed info from cache
			if 'lines_in_blob' not in self.cache:
				blobs_to_read.append((ext,blob_id))
				continue
			if blob_id in self.cache['lines_in_blob']:
				self.code_analysis['extensions'][ext]['lines'] += self.cache['lines_in_blob'][blob_id]
			else:
				blobs_to_read.append((ext,blob_id))

		#Get info about line count for new blobs using chunked processing
		ext_blob_linecount = []
		if blobs_to_read:
			worker_count = min(len(blobs_to_read), conf['processes'])
			chunk_size = conf.get('chunk_size', 500)
			if conf['verbose']:
				logger.info(f'Processing {len(blobs_to_read)} uncached blobs with {worker_count} workers (chunk size: {chunk_size})')

			for i in range(0, len(blobs_to_read), chunk_size):
				chunk = blobs_to_read[i:i + chunk_size]
				with ThreadPoolExecutor(max_workers=worker_count) as executor:
					chunk_results = list(executor.map(getnumoflinesinblob, chunk))
				ext_blob_linecount.extend(chunk_results)
				del chunk_results
				self._gc_if_needed()

		# SLOC analysis for ALL blobs using chunked processing
		ext_blob_sloc = []
		if all_blobs_for_sloc:
			worker_count = min(len(all_blobs_for_sloc), conf['processes'])
			chunk_size = conf.get('chunk_size', 500)
			if conf['verbose']:
				logger.info(f'Performing SLOC analysis on {len(all_blobs_for_sloc)} blobs with {worker_count} workers (chunk size: {chunk_size})')

			for i in range(0, len(all_blobs_for_sloc), chunk_size):
				chunk = all_blobs_for_sloc[i:i + chunk_size]
				with ThreadPoolExecutor(max_workers=worker_count) as executor:
					chunk_results = list(executor.map(analyzesloc, chunk))
				ext_blob_sloc.extend(chunk_results)
				del chunk_results
				self._gc_if_needed()

		#Update cache and write down info about number of number of lines
		for (ext, blob_id, linecount) in ext_blob_linecount:
			if 'lines_in_blob' not in self.cache:
				self.cache['lines_in_blob'] = {}
			self.cache['lines_in_blob'][blob_id] = linecount
			self.code_analysis['extensions'][ext]['lines'] += self.cache['lines_in_blob'][blob_id]

		# Update SLOC statistics
		for (ext, blob_id, total_lines, source_lines, comment_lines, blank_lines) in ext_blob_sloc:
			# Initialize extension SLOC tracking
			if ext not in self.code_analysis['sloc_by_extension']:
				self.code_analysis['sloc_by_extension'][ext] = {'source': 0, 'comments': 0, 'blank': 0, 'total': 0}

			# Update extension SLOC counts
			self.code_analysis['sloc_by_extension'][ext]['source'] += source_lines
			self.code_analysis['sloc_by_extension'][ext]['comments'] += comment_lines
			self.code_analysis['sloc_by_extension'][ext]['blank'] += blank_lines
			self.code_analysis['sloc_by_extension'][ext]['total'] += total_lines

			# Update global SLOC counts
			self.code_analysis['total_source_lines'] += source_lines
			self.code_analysis['total_comment_lines'] += comment_lines
			self.code_analysis['total_blank_lines'] += blank_lines



		# File revision counting
		logger.info('Collecting file revision statistics...')
		first_parent_flag = get_first_parent_flag()
		revision_lines = getpipeoutput(['git log %s --name-only --pretty=format: %s' % (first_parent_flag, getlogrange('HEAD'))]).strip().split('\n')
		for line in revision_lines:
			line = line.strip()
			if len(line) > 0 and not line.startswith('commit'):
				# This is a filename
				filename = line.split('/')[-1]  # Get just the filename for extension check

				# Apply extension filtering
				if not should_include_file(filename):
					continue

				if line not in self.code_analysis['file_revisions']:
					self.code_analysis['file_revisions'][line] = 0
				self.code_analysis['file_revisions'][line] += 1

				# Track directory activity
				directory = os.path.dirname(line) if os.path.dirname(line) else '.'
				self.code_analysis['directory_revisions'][directory] += 1
				self.code_analysis['directories'][directory]['files'].add(line)

		# Directory activity analysis
		logger.info('Collecting directory activity statistics...')
		first_parent_flag = get_first_parent_flag()
		numstat_lines = getpipeoutput(['git log %s --numstat --pretty=format:"%%at %%aN" %s' % (first_parent_flag, getlogrange('HEAD'))]).split('\n')
		current_author = None
		current_timestamp = None

		for line in numstat_lines:
			line = line.strip()
			if not line:
				continue

			# Check if this is a commit header line (timestamp + author)
			if line.count('\t') == 0 and ' ' in line:
				try:
					parts = line.split(' ', 1)
					current_timestamp = int(parts[0])
					current_author = parts[1]
					continue
				except (ValueError, IndexError):
					pass

			# Check if this is a numstat line (additions\tdeletions\tfilename)
			if line.count('\t') >= 2:
				parts = line.split('\t')
				if len(parts) >= 3:
					try:
						additions = int(parts[0]) if parts[0] != '-' else 0
						deletions = int(parts[1]) if parts[1] != '-' else 0
						filename = '\t'.join(parts[2:])  # Handle filenames with tabs

						# Apply extension filtering
						file_basename = filename.split('/')[-1]
						if not should_include_file(file_basename):
							continue

						# Track directory activity
						directory = os.path.dirname(filename) if os.path.dirname(filename) else '.'
						self.code_analysis['directories'][directory]['commits'] += 1  # Will be deduplicated later
						self.code_analysis['directories'][directory]['lines_added'] += additions
						self.code_analysis['directories'][directory]['lines_removed'] += deletions
						self.code_analysis['directories'][directory]['files'].add(filename)
					except ValueError:
						pass

		# line statistics
		# outputs:
		#  N files changed, N insertions (+), N deletions(-)
		# <stamp> <author>
		self.temporal_data['changes_by_date'] = {} # stamp -> { files, ins, del }
		# computation of lines of code by date is better done
		# on a linear history.
		extra = ''
		if conf['linear_linestats']:
			extra = '--first-parent -m'

		# Add --first-parent if scanning only default branch
		first_parent_flag = get_first_parent_flag()
		if first_parent_flag and not extra:
			extra = first_parent_flag
		elif first_parent_flag and extra:
			extra = f'{first_parent_flag} {extra}'

		lines = getpipeoutput(['git log --shortstat %s --pretty=format:"%%at %%aN" %s' % (extra, getlogrange('HEAD'))]).split('\n')
		lines.reverse()
		files = 0; inserted = 0; deleted = 0; total_lines = 0
		author = None
		for line in lines:
			if len(line) == 0:
				continue

			# <stamp> <author>
			if re.search('files? changed', line) == None:
				pos = line.find(' ')
				if pos != -1:
					try:
						(stamp, author) = (int(line[:pos]), line[pos+1:])
						self.temporal_data['changes_by_date'][stamp] = { 'files': files, 'ins': inserted, 'del': deleted, 'lines': total_lines }

						# Track pace of changes (total line changes)
						self.temporal_data['pace_of_changes'][stamp] = inserted + deleted

						date = datetime.datetime.fromtimestamp(stamp)

						# Track pace of changes by month and year
						yymm = date.strftime('%Y-%m')
						yy = date.year
						self.temporal_data['pace_of_changes_by_month'][yymm] += inserted + deleted
						self.temporal_data['pace_of_changes_by_year'][yy] += inserted + deleted

						# Track last 30 days activity
						import time as time_mod
						now = time_mod.time()
						if now - stamp <= 30 * 24 * 3600:  # 30 days in seconds
							self.recent_activity['last_30_days_commits'] += 1
							self.recent_activity['last_30_days_lines_added'] += inserted
							self.recent_activity['last_30_days_lines_removed'] += deleted

						# Track last 12 months activity
						if now - stamp <= 365 * 24 * 3600:  # 12 months in seconds
							yymm = date.strftime('%Y-%m')
							self.recent_activity['last_12_months_commits'][yymm] += 1
							self.recent_activity['last_12_months_lines_added'][yymm] += inserted
							self.recent_activity['last_12_months_lines_removed'][yymm] += deleted

						yymm = date.strftime('%Y-%m')
						self.temporal_data['lines_added_by_month'][yymm] += inserted
						self.temporal_data['lines_removed_by_month'][yymm] += deleted

						yy = date.year
						self.temporal_data['lines_added_by_year'][yy] += inserted
						self.temporal_data['lines_removed_by_year'][yy] += deleted

						files, inserted, deleted = 0, 0, 0
					except ValueError:
						logger.info('Warning: unexpected line "%s"' % line)
				else:
					logger.info('Warning: unexpected line "%s"' % line)
			else:
				numbers = getstatsummarycounts(line)

				if len(numbers) == 3:
					(files, inserted, deleted) = list(map(lambda el : int(el), numbers))
					total_lines += inserted
					total_lines -= deleted
					self.repository_stats['total_lines_added'] += inserted
					self.repository_stats['total_lines_removed'] += deleted

				else:
					logger.info('Warning: failed to handle line "%s"' % line)
					(files, inserted, deleted) = (0, 0, 0)
				#self.changes_by_date[stamp] = { 'files': files, 'ins': inserted, 'del': deleted }
		self.repository_stats['total_lines'] += total_lines

		# Per-author statistics

		# defined for stamp, author only if author commited at this timestamp.
		self.changes_by_date_by_author = {} # stamp -> author -> lines_added

		# Similar to the above, but add --first-parent if configured to scan default branch only
		# When scanning default branch only, we only want commits from the main line
		first_parent_flag = get_first_parent_flag()
		lines = getpipeoutput(['git log %s --shortstat --date-order --pretty=format:"%%at %%aN" %s' % (first_parent_flag, getlogrange('HEAD'))]).split('\n')
		lines.reverse()
		files = 0; inserted = 0; deleted = 0
		author = None
		stamp = 0
		for line in lines:
			if len(line) == 0:
				continue

			# <stamp> <author>
			if re.search('files? changed', line) == None:
				pos = line.find(' ')
				if pos != -1:
					try:
						oldstamp = stamp
						(stamp, author) = (int(line[:pos]), line[pos+1:])
						if oldstamp > stamp:
							# clock skew, keep old timestamp to avoid having ugly graph
							stamp = oldstamp
						if author not in self.authors:
							self.authors[author] = { 'lines_added' : 0, 'lines_removed' : 0, 'commits' : 0}
						self.authors[author]['commits'] += 1
						self.authors[author]['lines_added'] += inserted
						self.authors[author]['lines_removed'] += deleted
						if stamp not in self.changes_by_date_by_author:
							self.changes_by_date_by_author[stamp] = {}
						if author not in self.changes_by_date_by_author[stamp]:
							self.changes_by_date_by_author[stamp][author] = {}
						self.changes_by_date_by_author[stamp][author]['lines_added'] = self.authors[author]['lines_added']
						self.changes_by_date_by_author[stamp][author]['commits'] = self.authors[author]['commits']

						# Track author data by year
						date = datetime.datetime.fromtimestamp(stamp)
						year = date.year
						self.temporal_data['lines_added_by_author_by_year'][year][author] += inserted
						self.temporal_data['commits_by_author_by_year'][year][author] += 1
						files, inserted, deleted = 0, 0, 0
					except ValueError:
						logger.info('Warning: unexpected line "%s"' % line)
				else:
					logger.info('Warning: unexpected line "%s"' % line)
			else:
				numbers = getstatsummarycounts(line)

				if len(numbers) == 3:
					(files, inserted, deleted) = list(map(lambda el : int(el), numbers))
				else:
					logger.info('Warning: failed to handle line "%s"' % line)
					(files, inserted, deleted) = (0, 0, 0)

		# Branch analysis - collect unmerged branches and per-branch statistics
		if conf['verbose']:
			logger.info('Analyzing branches and detecting unmerged branches...')
		self._analyzeBranches()

		# Calculate repository size (this is slow as noted in TODO)
		if conf['verbose']:
			logger.info('Calculating repository size...')
		try:
			# Get .git directory size
			git_dir_size = getpipeoutput(['du -sm .git']).split()[0]
			self.repository_size_mb = float(git_dir_size)
			if conf['verbose']:
				logger.info(f'Repository size: {self.repository_size_mb:.1f} MB')
		except (ValueError, IndexError):
			logger.info('Warning: Could not calculate repository size')
			self.repository_size_mb = 0.0

		# Perform advanced team analysis
		self._analyzeTeamCollaboration()
		self._analyzeCommitPatterns()
		self._analyzeWorkingPatterns()
		self._analyzeImpactAndQuality()
		self._calculateTeamPerformanceMetrics()

	def _gc_if_needed(self):
		"""Run garbage collection if memory usage exceeds threshold."""
		gc_threshold = conf.get('gc_threshold_mb', 512)
		try:
			import psutil
			mem_mb = psutil.Process().memory_info().rss / 1024 / 1024
			if mem_mb > gc_threshold:
				gc.collect()
				if conf['verbose']:
					new_mem = psutil.Process().memory_info().rss / 1024 / 1024
					logger.info(f'GC triggered at {mem_mb:.0f}MB, now {new_mem:.0f}MB')
				return True
		except ImportError:
			# psutil not available, do periodic GC anyway in low_memory_mode
			if conf.get('low_memory_mode', False):
				gc.collect()
				return True
		return False

	def _detectMainBranch(self):
		"""Detect the main branch (master, main, develop, etc.)"""
		# Try common main branch names in order of preference
		main_branch_candidates = ['master', 'main', 'develop', 'development']

		# Get all local branches
		branches_output = getpipeoutput(['git branch'])
		local_branches = [line.strip().lstrip('* ') for line in branches_output.split('\n') if line.strip()]

		# Check if any of the common main branches exist
		for candidate in main_branch_candidates:
			if candidate in local_branches:
				self.main_branch = candidate
				return candidate

		# If none found, use the first branch or fall back to 'master'
		if local_branches:
			self.main_branch = local_branches[0]
			return local_branches[0]

		# Fall back to master
		self.main_branch = 'master'
		return 'master'

	def _analyzeBranches(self):
		"""Analyze all branches and detect unmerged ones"""
		try:
			# Detect main branch
			main_branch = self._detectMainBranch()
			if conf['verbose']:
				logger.info(f'Detected main branch: {main_branch}')

			# Get all local branches
			branches_output = getpipeoutput(['git branch'])
			all_branches = [line.strip().lstrip('* ') for line in branches_output.split('\n') if line.strip()]

			# Get unmerged branches (branches not merged into main)
			try:
				unmerged_output = getpipeoutput([f'git branch --no-merged {main_branch}'])
				self.unmerged_branches = [line.strip().lstrip('* ') for line in unmerged_output.split('\n')
										if line.strip() and not line.strip().startswith('*')]
			except:
				# If main branch doesn't exist or command fails, assume all branches are unmerged
				self.unmerged_branches = [b for b in all_branches if b != main_branch]

			if conf['verbose']:
				logger.info(f'Found {len(self.unmerged_branches)} unmerged branches: {", ".join(self.unmerged_branches)}')

			# Analyze each branch
			for branch in all_branches:
				if conf['verbose']:
					logger.info(f'Analyzing branch: {branch}')
				self._analyzeBranch(branch, main_branch)

		except Exception as e:
			if conf['verbose'] or conf['debug']:
				logger.info(f'Warning: Branch analysis failed: {e}')
			# Initialize empty structures if analysis fails
			self.unmerged_branches = []
			self.branches = {}

	def _analyzeBranch(self, branch_name, main_branch):
		"""Analyze a single branch for commits, authors, and line changes"""
		try:
			# Initialize branch data
			self.branches[branch_name] = {
				'commits': 0,
				'lines_added': 0,
				'lines_removed': 0,
				'authors': {},
				'is_merged': branch_name not in self.unmerged_branches,
				'merge_base': '',
				'unique_commits': []
			}

			# Get merge base with main branch
			try:
				merge_base = getpipeoutput([f'git merge-base {branch_name} {main_branch}']).strip()
				self.branches[branch_name]['merge_base'] = merge_base
			except:
				self.branches[branch_name]['merge_base'] = ''

			# Get commits unique to this branch (not in main branch)
			if branch_name != main_branch:
				try:
					# Get commits that are in branch but not in main
					unique_commits_output = getpipeoutput([f'git rev-list {branch_name} ^{main_branch}'])
					unique_commits = [line.strip() for line in unique_commits_output.split('\n') if line.strip()]
					self.branches[branch_name]['unique_commits'] = unique_commits

					# Analyze each unique commit
					for commit in unique_commits:
						self._analyzeBranchCommit(branch_name, commit)

				except:
					# If command fails, analyze all commits in the branch
					try:
						all_commits_output = getpipeoutput([f'git rev-list {branch_name}'])
						all_commits = [line.strip() for line in all_commits_output.split('\n') if line.strip()]
						self.branches[branch_name]['unique_commits'] = all_commits[:50]  # Limit to avoid too much data

						for commit in all_commits[:50]:
							self._analyzeBranchCommit(branch_name, commit)
					except:
						pass
			else:
				# For main branch, count all commits
				try:
					all_commits_output = getpipeoutput([f'git rev-list {branch_name}'])
					all_commits = [line.strip() for line in all_commits_output.split('\n') if line.strip()]
					self.branches[branch_name]['commits'] = len(all_commits)
					self.branches[branch_name]['unique_commits'] = all_commits[:100]  # Limit for performance
				except:
					pass

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Failed to analyze branch {branch_name}: {e}')

	def _analyzeBranchCommit(self, branch_name, commit_hash):
		"""Analyze a single commit for branch statistics"""
		try:
			# Get commit author and timestamp
			commit_info = getpipeoutput([f'git log -1 --pretty=format:"%aN %at" {commit_hash}'])
			if not commit_info:
				return

			parts = commit_info.rsplit(' ', 1)
			if len(parts) != 2:
				return

			author = parts[0]
			try:
				timestamp = int(parts[1])
			except ValueError:
				return

			# Update branch commit count
			self.branches[branch_name]['commits'] += 1

			# Update author statistics for this branch
			if author not in self.branches[branch_name]['authors']:
				self.branches[branch_name]['authors'][author] = {
					'commits': 0,
					'lines_added': 0,
					'lines_removed': 0
				}
			self.branches[branch_name]['authors'][author]['commits'] += 1

			# Get line changes for this commit
			try:
				numstat_output = getpipeoutput([f'git show --numstat --format="" {commit_hash}'])
				for line in numstat_output.split('\n'):
					if line.strip() and '\t' in line:
						parts = line.split('\t')
						if len(parts) >= 2:
							try:
								additions = int(parts[0]) if parts[0] != '-' else 0
								deletions = int(parts[1]) if parts[1] != '-' else 0

								# Update branch statistics
								self.branches[branch_name]['lines_added'] += additions
								self.branches[branch_name]['lines_removed'] += deletions

								# Update author statistics for this branch
								self.branches[branch_name]['authors'][author]['lines_added'] += additions
								self.branches[branch_name]['authors'][author]['lines_removed'] += deletions

							except ValueError:
								pass
			except:
				pass

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Failed to analyze commit {commit_hash}: {e}')

	def _analyzeTeamCollaboration(self):
		"""Analyze how team members collaborate on files and projects"""
		if conf['verbose']:
			logger.info('Analyzing team collaboration patterns...')

		try:
			# Get commit details with files changed
			log_range = getlogrange('HEAD')
			first_parent_flag = get_first_parent_flag()
			commit_data = getpipeoutput(['git log %s --name-only --pretty=format:"COMMIT:%%H:%%aN:%%at" %s' % (first_parent_flag, log_range)]).split('\n')

			current_commit = None
			current_author = None
			current_timestamp = None

			for line in commit_data:
				line = line.strip()
				if line.startswith('COMMIT:'):
					# Parse commit header: COMMIT:hash:author:timestamp
					parts = line.split(':', 3)
					if len(parts) >= 4:
						current_commit = parts[1]
						current_author = parts[2]
						try:
							current_timestamp = int(parts[3])
						except ValueError:
							current_timestamp = None
				elif line and current_author and not line.startswith('COMMIT:'):
					# This is a filename
					filename = line

					# Apply extension filtering
					file_basename = filename.split('/')[-1]
					if not should_include_file(file_basename):
						continue

					# Initialize author collaboration data
					if current_author not in self.author_collaboration:
						self.author_collaboration[current_author] = {
							'worked_with': defaultdict(lambda: defaultdict(int)),
							'file_ownership': defaultdict(int)
						}

					# Track file ownership
					self.author_collaboration[current_author]['file_ownership'][filename] += 1

					# Track who else worked on this file
					file_history = getpipeoutput([f'git log --pretty=format:"%aN" -- "{filename}"']).split('\n')
					unique_authors = set(file_history) - {current_author}

					for other_author in unique_authors:
						if other_author.strip():
							self.author_collaboration[current_author]['worked_with'][other_author][filename] += 1

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Team collaboration analysis failed: {e}')

		# Enhanced Metrics Collection - College Project Implementation
		logger.info('Calculating enhanced metrics...')

		# Analyze current files for enhanced metrics
		try:
			# Get list of all tracked files
			files_output = getpipeoutput(['git ls-files'])
			if files_output.strip():
				tracked_files = files_output.strip().split('\n')

				# Analyze each file for documentation and complexity
				for filepath in tracked_files[:100]:  # Limit to first 100 files for performance
					if os.path.exists(filepath):
						self.update_enhanced_metrics(filepath)

				# Check for README and documentation files
				readme_files = [f for f in tracked_files if 'readme' in f.lower() or 'doc' in f.lower()]
				self.documentation_metrics['readme_sections'] = len(readme_files) * 5  # Basic scoring

				# Count documentation files
				doc_extensions = ['.md', '.txt', '.rst', '.doc']
				self.documentation_metrics['total_documentation_files'] = sum(
					1 for f in tracked_files
					if any(f.lower().endswith(ext) for ext in doc_extensions)
				)

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Enhanced file analysis failed: {e}')

		# Calculate comprehensive software metrics
		try:
			if conf['verbose']:
				logger.info('Calculating comprehensive software metrics...')
			self._calculate_comprehensive_project_metrics()
		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Comprehensive metrics calculation failed: {e}')



	def _analyzeCommitPatterns(self):
		"""Analyze commit patterns to identify commit behavior (small vs large commits, frequency, etc.)"""
		if conf['verbose']:
			logger.info('Analyzing commit patterns...')

		try:
			# Get detailed commit information using a simpler, more reliable approach
			log_range = getlogrange('HEAD')
			first_parent_flag = get_first_parent_flag()
			commit_lines = getpipeoutput(['git log %s --shortstat --pretty=format:"COMMIT:%%H:%%aN:%%at:%%s" %s' % (first_parent_flag, log_range)]).split('\n')

			current_author = None
			current_timestamp = None
			current_message = None
			author_commits = defaultdict(list)

			for line in commit_lines:
				line = line.strip()
				if line.startswith('COMMIT:'):
					# Parse: COMMIT:hash:author:timestamp:subject
					parts = line.split(':', 4)
					if len(parts) >= 5:
						current_author = parts[2]
						try:
							current_timestamp = int(parts[3])
							current_message = parts[4] if len(parts) > 4 else ""
						except (ValueError, IndexError):
							current_timestamp = None
							current_message = ""
				elif line and current_author and re.search(r'files? changed', line):
					# Parse shortstat line: "1 file changed, 269 insertions(+), 91 deletions(-)"
					numbers = re.findall(r'\d+', line)
					if len(numbers) >= 1:
						files_changed = int(numbers[0])
						insertions = int(numbers[1]) if len(numbers) > 1 else 0
						deletions = int(numbers[2]) if len(numbers) > 2 else 0
						total_changes = insertions + deletions

						commit_info = {
							'timestamp': current_timestamp,
							'files_changed': files_changed,
							'lines_changed': total_changes,
							'insertions': insertions,
							'deletions': deletions,
							'message': current_message if current_message else ""
						}
						author_commits[current_author].append(commit_info)

			# Analyze patterns for each author
			for author, commits in author_commits.items():
				if not commits:
					continue

				total_commits = len(commits)
				total_lines = sum(c['lines_changed'] for c in commits)
				avg_commit_size = total_lines / total_commits if total_commits else 0

				# Categorize commits by size
				small_commits = sum(1 for c in commits if c['lines_changed'] < 10)
				medium_commits = sum(1 for c in commits if 10 <= c['lines_changed'] < 100)
				large_commits = sum(1 for c in commits if c['lines_changed'] >= 100)

				# Calculate commit frequency (commits per day)
				if commits:
					timestamps = [c['timestamp'] for c in commits if c['timestamp']]
					if len(timestamps) > 1:
						time_span = max(timestamps) - min(timestamps)
						days_active = time_span / (24 * 3600) if time_span > 0 else 1
						commit_frequency = total_commits / days_active
					else:
						commit_frequency = total_commits
				else:
					commit_frequency = 0

				# Analyze commit messages for patterns
				bug_related = sum(1 for c in commits if any(keyword in c['message'].lower()
					for keyword in ['fix', 'bug', 'error', 'issue', 'patch', 'repair']))
				feature_related = sum(1 for c in commits if any(keyword in c['message'].lower()
					for keyword in ['add', 'new', 'feature', 'implement', 'create']))
				refactor_related = sum(1 for c in commits if any(keyword in c['message'].lower()
					for keyword in ['refactor', 'cleanup', 'reorganize', 'restructure', 'optimize']))

				self.team_analysis['commit_patterns'][author] = {
					'total_commits': total_commits,
					'avg_commit_size': avg_commit_size,
					'small_commits': small_commits,
					'medium_commits': medium_commits,
					'large_commits': large_commits,
					'commit_frequency': commit_frequency,
					'bug_related_commits': bug_related,
					'feature_related_commits': feature_related,
					'refactor_related_commits': refactor_related,
					'avg_files_per_commit': sum(c['files_changed'] for c in commits) / total_commits if total_commits else 0
				}

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Commit pattern analysis failed: {e}')

	def _analyzeWorkingPatterns(self):
		"""Analyze when authors typically work (time of day, day of week, timezone patterns)"""
		if conf['verbose']:
			logger.info('Analyzing working time patterns...')

		try:
			# Get commit timestamps with timezone info
			log_range = getlogrange('HEAD')
			first_parent_flag = get_first_parent_flag()
			commit_lines = getpipeoutput(['git log %s --pretty=format:"%%aN|%%at|%%ai|%%s" %s' % (first_parent_flag, log_range)]).split('\n')

			for line in commit_lines:
				if not line.strip():
					continue

				parts = line.split('|', 3)
				if len(parts) < 3:
					continue

				author = parts[0]
				try:
					timestamp = int(parts[1])
					date_str = parts[2]  # ISO format with timezone
					message = parts[3] if len(parts) > 3 else ""
				except (ValueError, IndexError):
					continue

				# Parse date and time information
				date = datetime.datetime.fromtimestamp(timestamp)
				hour = date.hour
				day_of_week = date.weekday()  # Monday = 0, Sunday = 6

				# Initialize author working patterns
				if author not in self.team_analysis['working_patterns']:
					self.team_analysis['working_patterns'][author] = {
						'night_commits': 0,      # 22:00 - 06:00
						'weekend_commits': 0,    # Saturday, Sunday
						'peak_hours': defaultdict(int),
						'peak_days': defaultdict(int),
						'timezone_pattern': defaultdict(int),
						'early_bird': 0,         # 05:00 - 09:00
						'workday': 0,           # 09:00 - 17:00
						'evening': 0,           # 17:00 - 22:00
						'total_commits': 0
					}

				self.team_analysis['working_patterns'][author]['total_commits'] += 1
				self.team_analysis['working_patterns'][author]['peak_hours'][hour] += 1
				self.team_analysis['working_patterns'][author]['peak_days'][day_of_week] += 1

				# Extract timezone from date string
				if '+' in date_str or '-' in date_str:
					tz_part = date_str.split()[-1]
					self.team_analysis['working_patterns'][author]['timezone_pattern'][tz_part] += 1

				# Categorize by time of day
				if 22 <= hour or hour < 6:
					self.team_analysis['working_patterns'][author]['night_commits'] += 1
				elif 5 <= hour < 9:
					self.team_analysis['working_patterns'][author]['early_bird'] += 1
				elif 9 <= hour < 17:
					self.team_analysis['working_patterns'][author]['workday'] += 1
				elif 17 <= hour < 22:
					self.team_analysis['working_patterns'][author]['evening'] += 1

				# Weekend commits (Saturday = 5, Sunday = 6)
				if day_of_week >= 5:
					self.team_analysis['working_patterns'][author]['weekend_commits'] += 1

				# Classify commit types
				if any(keyword in message.lower() for keyword in ['fix', 'bug', 'error', 'patch']):
					if author not in self.commit_categories['bug_commits']:
						self.commit_categories['bug_commits'].append({'author': author, 'timestamp': timestamp, 'message': message})
				elif any(keyword in message.lower() for keyword in ['refactor', 'cleanup', 'optimize']):
					self.refactoring_commits.append({'author': author, 'timestamp': timestamp, 'message': message})
				elif any(keyword in message.lower() for keyword in ['add', 'new', 'feature', 'implement']):
					self.feature_commits.append({'author': author, 'timestamp': timestamp, 'message': message})

			# Calculate active periods for each author
			for author in self.authors:
				if 'active_days' in self.authors[author]:
					active_days = self.authors[author]['active_days']
					sorted_days = sorted(active_days)

					if len(sorted_days) > 1:
						# Calculate gaps between active days
						gaps = []
						for i in range(1, len(sorted_days)):
							prev_date = datetime.datetime.strptime(sorted_days[i-1], '%Y-%m-%d')
							curr_date = datetime.datetime.strptime(sorted_days[i], '%Y-%m-%d')
							gap = (curr_date - prev_date).days
							gaps.append(gap)

						avg_gap = sum(gaps) / len(gaps) if gaps else 0

						# Find longest streak
						longest_streak = 1
						current_streak = 1
						for gap in gaps:
							if gap == 1:
								current_streak += 1
								longest_streak = max(longest_streak, current_streak)
							else:
								current_streak = 1
					else:
						avg_gap = 0
						longest_streak = 1

					self.author_active_periods[author] = {
						'active_days_count': len(active_days),
						'longest_streak': longest_streak,
						'avg_gap': avg_gap
					}

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Working pattern analysis failed: {e}')

	def _analyzeImpactAndQuality(self):
		"""Analyze the impact of changes and identify critical files and potential quality issues"""
		if conf['verbose']:
			logger.info('Analyzing impact and quality indicators...')

		try:
			# Identify critical files based on common patterns
			all_files = getpipeoutput(['git ls-tree -r --name-only %s' % getcommitrange('HEAD', end_only=True)]).split('\n')

			for filepath in all_files:
				if not filepath.strip():
					continue

				filename = os.path.basename(filepath)
				filename_lower = filename.lower()

				# Apply extension filtering
				if not should_include_file(filename):
					continue

				# Mark files as critical based on allowed extensions
				critical_patterns = {
					'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx', '.m', '.mm',
					'.swift', '.cu', '.cuh', '.cl', '.java', '.scala', '.kt', '.go', '.rs',
					'.py', '.pyi', '.pyx', '.pxd', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx',
					'.d.ts', '.lua', '.proto', '.thrift', '.asm', '.s', '.S', '.R', '.r'
				}

				# Check if file has a critical extension
				file_extension = None
				if '.' in filename:
					file_extension = '.' + filename.split('.')[-1].lower()
					# Handle special cases like .d.ts
					if filename.lower().endswith('.d.ts'):
						file_extension = '.d.ts'

				if file_extension and file_extension in critical_patterns:
					self.critical_files.add(filepath)

				# Files in root directory are often critical
				if '/' not in filepath:
					self.critical_files.add(filepath)

			# Analyze file impact scores based on change frequency and author diversity
			file_authors = defaultdict(set)
			file_change_count = defaultdict(int)

			# Get file change history
			log_range = getlogrange('HEAD')
			first_parent_flag = get_first_parent_flag()
			log_lines = getpipeoutput(['git log %s --name-only --pretty=format:"AUTHOR:%%aN" %s' % (first_parent_flag, log_range)]).split('\n')
			current_author = None

			for line in log_lines:
				line = line.strip()
				if line.startswith('AUTHOR:'):
					current_author = line.replace('AUTHOR:', '')
				elif line and current_author and not line.startswith('AUTHOR:'):
					filename = line

					# Apply extension filtering
					file_basename = filename.split('/')[-1]
					if not should_include_file(file_basename):
						continue

					file_authors[filename].add(current_author)
					file_change_count[filename] += 1

			# Calculate impact scores
			for filename in file_change_count:
				change_count = file_change_count[filename]
				author_count = len(file_authors[filename])

				# Impact score based on change frequency and author diversity
				base_score = min(change_count * 10, 100)  # Cap at 100
				diversity_bonus = min(author_count * 5, 25)  # Bonus for multiple authors
				critical_bonus = 50 if filename in self.critical_files else 0

				impact_score = base_score + diversity_bonus + critical_bonus
				self.file_impact_scores[filename] = impact_score

			# Analyze author impact
			for author in self.authors:
				critical_files_touched = []
				total_impact_score = 0

				# Check which critical files this author touched
				for filename in self.critical_files:
					if author in file_authors.get(filename, set()):
						critical_files_touched.append(filename)
						total_impact_score += self.file_impact_scores.get(filename, 0)

				# Calculate maintenance work ratio (neutral term for bug fixes)
				author_commits = self.team_analysis['commit_patterns'].get(author, {})
				maintenance_commits = author_commits.get('bug_related_commits', 0)
				total_commits = author_commits.get('total_commits', 1)
				maintenance_ratio = maintenance_commits / total_commits if total_commits > 0 else 0

				# Maintenance work percentage (neutral metric, not "bug potential")
				maintenance_percentage = min(maintenance_ratio * 100, 100)

				self.impact_analysis[author] = {
					'critical_files': critical_files_touched,
					'impact_score': total_impact_score,
					'maintenance_percentage': maintenance_percentage,
					'high_impact_files': [f for f in file_authors if author in file_authors[f] and self.file_impact_scores.get(f, 0) > 50]
				}

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Impact analysis failed: {e}')

	def _calculateTeamPerformanceMetrics(self):
		"""Calculate comprehensive team performance metrics"""
		if conf['verbose']:
			logger.info('Calculating team performance metrics...')

		try:
			total_commits = self.getTotalCommits()
			total_lines_changed = self.repository_stats['total_lines_added'] + self.repository_stats['total_lines_removed']

			for author in self.authors:
				author_info = self.authors[author]
				commit_patterns = self.team_analysis['commit_patterns'].get(author, {})
				working_patterns = self.team_analysis['working_patterns'].get(author, {})
				impact_info = self.impact_analysis.get(author, {})

				# Activity Score (based on commit patterns and code modification metrics)
				avg_commit_size = commit_patterns.get('avg_commit_size', 0)
				total_author_commits = author_info.get('commits', 0)

				# Commit size distribution scoring (no penalties, just measurement)
				if 20 <= avg_commit_size <= 50:
					size_score = 100  # Moderate-sized commits
				elif avg_commit_size < 20:
					size_score = max(0, avg_commit_size * 5)  # Small incremental commits
				else:
					size_score = max(0, 100 - (avg_commit_size - 50) * 2)  # Larger commits

				# Work type distribution (all types valued equally)
				maintenance_commits = commit_patterns.get('bug_related_commits', 0)
				feature_commits = commit_patterns.get('feature_related_commits', 0)
				refactor_commits = commit_patterns.get('refactor_related_commits', 0)

				work_diversity_score = 0
				if total_author_commits > 0:
					feature_ratio = feature_commits / total_author_commits
					refactor_ratio = refactor_commits / total_author_commits
					maintenance_ratio = maintenance_commits / total_author_commits

					# All work types contribute positively (no penalties for maintenance)
					work_diversity_score = (feature_ratio * 35 + refactor_ratio * 35 + maintenance_ratio * 30) * 100
					work_diversity_score = max(0, min(100, work_diversity_score))

				activity_score = (size_score * 0.6 + work_diversity_score * 0.4)

				# Consistency Score (based on commit frequency and working patterns)
				commit_frequency = commit_patterns.get('commit_frequency', 0)
				active_periods = self.author_active_periods.get(author, {})
				longest_streak = active_periods.get('longest_streak', 1)
				avg_gap = active_periods.get('avg_gap', 30)

				# Consistency based on regular commits and sustained activity
				frequency_score = min(commit_frequency * 20, 100)  # Up to 5 commits per day = max score
				streak_score = min(longest_streak * 5, 100)  # Longer streaks = better consistency
				gap_score = max(0, 100 - avg_gap * 3)  # Smaller gaps = better consistency

				consistency_score = (frequency_score * 0.4 + streak_score * 0.3 + gap_score * 0.3)

				# Collaboration Score (based on code interaction patterns and shared file work)
				impact_score = impact_info.get('impact_score', 0)
				critical_files_count = len(impact_info.get('critical_files', []))

				# Multi-author file collaboration based on working with others
				collaboration_data = self.author_collaboration.get(author, {})
				worked_with_count = len(collaboration_data.get('worked_with', {}))

				# Normalize collaboration metrics (objective measurement)
				impact_component = min(impact_score / 10, 100)  # Scale impact score
				multi_author_component = min(worked_with_count * 10, 100)  # Max score at 10 collaborators
				shared_files_component = min(critical_files_count * 20, 100)  # Max score at 5 critical files

				collaboration_score = (impact_component * 0.4 + multi_author_component * 0.3 + shared_files_component * 0.3)

				# Overall contribution percentage
				author_commits = author_info.get('commits', 0)
				contribution_percentage = (author_commits / total_commits * 100) if total_commits > 0 else 0

				# Store performance metrics
				self.team_performance[author] = {
					'activity_score': activity_score,
					'consistency': consistency_score,
					'collaboration_score': collaboration_score,
					'contribution_percentage': contribution_percentage,
					'overall_score': (activity_score * 0.4 + consistency_score * 0.3 + collaboration_score * 0.3),
					'commit_analysis': {
						'avg_commit_size': avg_commit_size,
						'small_commits_ratio': commit_patterns.get('small_commits', 0) / total_author_commits if total_author_commits > 0 else 0,
						'large_commits_ratio': commit_patterns.get('large_commits', 0) / total_author_commits if total_author_commits > 0 else 0,
						'maintenance_ratio': maintenance_commits / total_author_commits if total_author_commits > 0 else 0,
						'feature_ratio': feature_commits / total_author_commits if total_author_commits > 0 else 0
					}
				}

		except Exception as e:
			if conf['debug']:
				logger.info(f'Warning: Team performance calculation failed: {e}')

	def refine(self):
		# Calculate comprehensive metrics for all files
		logger.info('Calculating comprehensive code metrics...')
		self._calculate_comprehensive_project_metrics(getattr(self, 'repository_path', None))



		# authors
		# name -> {place_by_commits, commits_frac, date_first, date_last, timedelta}
		self.authors_by_commits = getkeyssortedbyvaluekey(self.authors, 'commits')
		self.authors_by_commits.reverse() # most first
		for i, name in enumerate(self.authors_by_commits):
			self.authors[name]['place_by_commits'] = i + 1

		for name in list(self.authors.keys()):
			a = self.authors[name]
			a['commits_frac'] = (100 * float(a['commits'])) / self.getTotalCommits()
			date_first = datetime.datetime.fromtimestamp(a['first_commit_stamp'])
			date_last = datetime.datetime.fromtimestamp(a['last_commit_stamp'])
			delta = date_last - date_first
			a['date_first'] = date_first.strftime('%Y-%m-%d')
			a['date_last'] = date_last.strftime('%Y-%m-%d')
			a['timedelta'] = delta
			if 'lines_added' not in a: a['lines_added'] = 0
			if 'lines_removed' not in a: a['lines_removed'] = 0

	def getActiveDays(self):
		return self.active_days

	def getActivityByDayOfWeek(self):
		return self.activity_by_day_of_week

	def getActivityByHourOfDay(self):
		return self.activity_by_hour_of_day

	def getAuthorInfo(self, author):
		return self.authors[author]

	def getAuthors(self, limit = None):
		res = getkeyssortedbyvaluekey(self.authors, 'commits')
		res.reverse()
		return res[:limit]

	def getCommitDeltaDays(self):
		return (self.last_commit_stamp // 86400 - self.first_commit_stamp // 86400) + 1

	def getDomainInfo(self, domain):
		return self.domains[domain]

	def getDomains(self):
		return list(self.domains.keys())

	def getFirstCommitDate(self):
		return datetime.datetime.fromtimestamp(self.first_commit_stamp)

	def getLastCommitDate(self):
		return datetime.datetime.fromtimestamp(self.last_commit_stamp)

	def getTags(self):
		lines = getpipeoutput(['git show-ref --tags', 'cut -d/ -f3'])
		return lines.split('\n')

	def getTagDate(self, tag):
		return self.revToDate('tags/' + tag)

	def getTotalAuthors(self):
		return self.total_authors

	def getTotalCommits(self):
		return self.repository_stats['total_commits']

	def getTotalFiles(self):
		return self.repository_stats['total_files']

	def getTotalLOC(self):
		return self.repository_stats['total_lines']

	def getTotalSourceLines(self):
		return self.code_analysis['total_source_lines']

	def getTotalCommentLines(self):
		return self.total_comment_lines

	def getTotalBlankLines(self):
		return self.total_blank_lines

	def getSLOCByExtension(self):
		return self.sloc_by_extension

	def getLargestFiles(self, limit=10):
		"""Get the largest files by size."""
		sorted_files = sorted(self.file_sizes.items(), key=lambda x: x[1], reverse=True)
		return sorted_files[:limit]

	def getFilesWithMostRevisions(self, limit=10):
		"""Get files with most revisions (hotspots)."""
		sorted_files = sorted(self.file_revisions.items(), key=lambda x: x[1], reverse=True)
		return sorted_files[:limit]

	def getAverageFileSize(self):
		"""Get average file size in bytes."""
		if not self.file_sizes:
			return 0.0
		return sum(self.file_sizes.values()) / len(self.file_sizes)

	def getDirectoriesByActivity(self, limit=10):
		"""Get directories with most lines changed (added + removed)."""
		if not hasattr(self, 'directories'):
			return []
		directory_activity = []
		for directory, stats in self.directories.items():
			total_lines = stats['lines_added'] + stats['lines_removed']
			file_count = len(stats['files'])
			directory_activity.append((directory, total_lines, stats['lines_added'], stats['lines_removed'], file_count))
		return sorted(directory_activity, key=lambda x: x[1], reverse=True)[:limit]

	def getDirectoriesByRevisions(self, limit=10):
		"""Get directories with most file revisions."""
		if not hasattr(self, 'directory_revisions'):
			return []
		sorted_dirs = sorted(self.directory_revisions.items(), key=lambda x: x[1], reverse=True)
		return sorted_dirs[:limit]

	def getAverageRevisionsPerFile(self):
		"""Get average number of revisions per file."""
		if not self.file_revisions:
			return 0.0
		return sum(self.file_revisions.values()) / len(self.file_revisions)

	def getTotalSize(self):
		return self.total_size

	def getLast30DaysActivity(self):
		"""Get activity stats for last 30 days."""
		return {
			'commits': self.last_30_days_commits,
			'lines_added': self.last_30_days_lines_added,
			'lines_removed': self.last_30_days_lines_removed
		}

	def getLast12MonthsActivity(self):
		"""Get activity stats for last 12 months."""
		return {
			'commits': dict(self.last_12_months_commits),
			'lines_added': dict(self.last_12_months_lines_added),
			'lines_removed': dict(self.last_12_months_lines_removed)
		}

	def getPaceOfChanges(self):
		"""Get pace of changes (line changes over time)."""
		return self.pace_of_changes

	def getPaceOfChangesByMonth(self):
		"""Get pace of changes by month."""
		return dict(self.pace_of_changes_by_month)

	def getPaceOfChangesByYear(self):
		"""Get pace of changes by year."""
		return dict(self.pace_of_changes_by_year)

	def getPaceOfChangesByWeek(self):
		"""Get pace of changes aggregated by week."""
		weekly_data = {}
		for stamp, changes in self.pace_of_changes.items():
			# Get the date from timestamp
			date = datetime.datetime.fromtimestamp(stamp)
			# Get Monday of the week (start of week)
			monday = date - datetime.timedelta(days=date.weekday())
			week_key = monday.strftime('%Y-%m-%d')

			if week_key not in weekly_data:
				weekly_data[week_key] = 0
			weekly_data[week_key] += changes

		return weekly_data

	def getFilesByYear(self):
		"""Get file count by year."""
		return dict(self.files_by_year)

	def getLinesOfCodeByYear(self):
		"""Get lines of code by year."""
		return dict(self.lines_added_by_year)

	def getLinesAddedByAuthorByYear(self):
		"""Get lines added by author by year."""
		return dict(self.lines_added_by_author_by_year)

	def getCommitsByAuthorByYear(self):
		"""Get commits by author by year."""
		return dict(self.commits_by_author_by_year)

	def getRepositorySize(self):
		"""Get repository size in MB."""
		return getattr(self, 'repository_size_mb', 0.0)

	def getBranches(self):
		"""Get all branches with their statistics."""
		return self.branches

	def getUnmergedBranches(self):
		"""Get list of unmerged branch names."""
		return self.unmerged_branches

	def getMainBranch(self):
		"""Get the detected main branch name."""
		return getattr(self, 'main_branch', 'master')

	def getBranchInfo(self, branch_name):
		"""Get detailed information about a specific branch."""
		return self.branches.get(branch_name, {})

	def getBranchAuthors(self, branch_name):
		"""Get authors who contributed to a specific branch."""
		branch_info = self.branches.get(branch_name, {})
		return branch_info.get('authors', {})

	def getBranchesByCommits(self, limit=None):
		"""Get branches sorted by number of commits."""
		sorted_branches = sorted(self.branches.items(),
								key=lambda x: x[1].get('commits', 0),
								reverse=True)
		if limit:
			return sorted_branches[:limit]
		return sorted_branches

	def getBranchesByLinesChanged(self, limit=None):
		"""Get branches sorted by total lines changed."""
		sorted_branches = sorted(self.branches.items(),
								key=lambda x: x[1].get('lines_added', 0) + x[1].get('lines_removed', 0),
								reverse=True)
		if limit:
			return sorted_branches[:limit]
		return sorted_branches

	def getUnmergedBranchStats(self):
		"""Get statistics for unmerged branches only."""
		unmerged_stats = {}
		for branch_name in self.unmerged_branches:
			if branch_name in self.branches:
				unmerged_stats[branch_name] = self.branches[branch_name]
		return unmerged_stats

	# New methods for advanced team analysis
	def getCommitPatterns(self):
		"""Get commit patterns analysis for all authors."""
		return self.commit_patterns

	def getCommitPatternsForAuthor(self, author):
		"""Get commit patterns for a specific author."""
		return self.team_analysis['commit_patterns'].get(author, {})

	def getWorkingPatterns(self):
		"""Get working time patterns for all authors."""
		return self.working_patterns

	def getWorkingPatternsForAuthor(self, author):
		"""Get working patterns for a specific author."""
		return self.working_patterns.get(author, {})

	def getTeamCollaboration(self):
		"""Get team collaboration analysis."""
		return self.author_collaboration

	def getCollaborationForAuthor(self, author):
		"""Get collaboration data for a specific author."""
		return self.author_collaboration.get(author, {})

	def getImpactAnalysis(self):
		"""Get impact analysis for all authors."""
		return self.impact_analysis

	def getImpactAnalysisForAuthor(self, author):
		"""Get impact analysis for a specific author."""
		return self.impact_analysis.get(author, {})

	def getTeamPerformance(self):
		"""Get team performance metrics for all authors."""
		return self.team_performance

	def getTeamPerformanceForAuthor(self, author):
		"""Get team performance metrics for a specific author."""
		return self.team_performance.get(author, {})

	def getCriticalFiles(self):
		"""Get list of files identified as critical to the project."""
		return list(self.critical_files)

	def getFileImpactScores(self):
		"""Get impact scores for all files."""
		return dict(self.file_impact_scores)

	def getTopImpactFiles(self, limit=10):
		"""Get files with highest impact scores."""
		sorted_files = sorted(self.file_impact_scores.items(), key=lambda x: x[1], reverse=True)
		return sorted_files[:limit]

	def getBugRelatedCommits(self):
		"""Get commits that appear to be bug-related."""
		return self.potential_bug_commits

	def getRefactoringCommits(self):
		"""Get commits that appear to be refactoring."""
		return self.refactoring_commits

	def getFeatureCommits(self):
		"""Get commits that appear to add features."""
		return self.feature_commits

	def getAuthorActivePeriods(self):
		"""Get active periods analysis for all authors."""
		return self.author_active_periods

	def getAuthorsByContribution(self):
		"""Get authors sorted by contribution percentage."""
		performance_data = [(author, perf.get('contribution_percentage', 0))
						   for author, perf in self.team_performance.items()]
		return sorted(performance_data, key=lambda x: x[1], reverse=True)

	def getAuthorsByActivity(self):
		"""Get authors sorted by activity score."""
		performance_data = [(author, perf.get('activity_score', 0))
						   for author, perf in self.team_performance.items()]
		return sorted(performance_data, key=lambda x: x[1], reverse=True)

	def getAuthorsByRegularity(self):
		"""Get authors sorted by activity regularity score."""
		performance_data = [(author, perf.get('consistency', 0))
						   for author, perf in self.team_performance.items()]
		return sorted(performance_data, key=lambda x: x[1], reverse=True)

	def getAuthorsByCollaboration(self):
		"""Get authors sorted by collaboration score."""
		performance_data = [(author, perf.get('collaboration_score', 0))
						   for author, perf in self.team_performance.items()]
		return sorted(performance_data, key=lambda x: x[1], reverse=True)

	def getTeamWorkDistribution(self):
		"""Analyze work distribution across team members."""
		total_commits = self.getTotalCommits()
		total_lines = self.total_lines_added + self.total_lines_removed

		distribution = {}
		for author in self.authors:
			author_info = self.authors[author]
			author_commits = author_info.get('commits', 0)
			author_lines = author_info.get('lines_added', 0) + author_info.get('lines_removed', 0)

			distribution[author] = {
				'commit_percentage': (author_commits / total_commits * 100) if total_commits > 0 else 0,
				'lines_percentage': (author_lines / total_lines * 100) if total_lines > 0 else 0,
				'commits': author_commits,
				'lines_changed': author_lines
			}

		return distribution

	def getCommitSizeAnalysis(self):
		"""Get analysis of commit sizes across the team."""
		analysis = {
			'small_commits_authors': [],  # Authors with >50% small commits
			'large_commits_authors': [],  # Authors with >20% large commits
			'balanced_authors': [],       # Authors with balanced commit sizes
			'overall_stats': {
				'total_small': 0,
				'total_medium': 0,
				'total_large': 0
			}
		}

		for author, patterns in self.commit_patterns.items():
			total_commits = patterns.get('total_commits', 0)
			if total_commits == 0:
				continue

			small_ratio = patterns.get('small_commits', 0) / total_commits
			large_ratio = patterns.get('large_commits', 0) / total_commits

			analysis['overall_stats']['total_small'] += patterns.get('small_commits', 0)
			analysis['overall_stats']['total_medium'] += patterns.get('medium_commits', 0)
			analysis['overall_stats']['total_large'] += patterns.get('large_commits', 0)

			if small_ratio > 0.5:
				analysis['small_commits_authors'].append((author, small_ratio))
			elif large_ratio > 0.2:
				analysis['large_commits_authors'].append((author, large_ratio))
			else:
				analysis['balanced_authors'].append((author, small_ratio, large_ratio))

		return analysis

	def revToDate(self, rev):
		stamp = int(getpipeoutput(['git log --pretty=format:%%at "%s" -n 1' % rev]))
		return datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')

