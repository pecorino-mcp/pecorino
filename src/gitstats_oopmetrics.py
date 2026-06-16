"""
GitStats OOP Metrics Module - Multi-Language AST Parser and OOP Metrics Analyzer

This module provides:
1. A custom AST-like parser for multi-language OOP construct detection
   (Python, Java, JavaScript/TypeScript, C++, Go, Rust, Swift)
2. OOP metrics analysis with focus on Distance from Main Sequence (D)

Key Metrics:
- Afferent Coupling (Ca): Classes outside depending on classes inside
- Efferent Coupling (Ce): Classes inside depending on classes outside
- Instability (I): Ce / (Ce + Ca)
- Abstractness (A): Abstract classes / Total classes
- Distance from Main Sequence (D): |A + I - 1|

Zone Interpretations:
- D < 0.2: Good - Well-balanced package design
- 0.2 <= D <= 0.4: Moderate - May need refactoring
- D > 0.4: Poor - Design issues (Zone of Pain or Zone of Uselessness)

Author: GitStats3 Enhancement
Date: 2025
"""

import re
from collections import defaultdict
from typing import Dict, List

from src.gitstats_ast import ClassDef, ImportDef, InterfaceDef, walk
from src.gitstats_tree_sitter_parser import parse_with_tree_sitter


def parse(source: str, extension: str = '.py'):
    return parse_with_tree_sitter(source, extension)

class OOPMetricsAnalyzer:
	"""Analyzer for Object-Oriented Programming metrics with focus on Distance from Main Sequence."""

	def __init__(self, use_ast: bool = True, repo_path: str = None):
		"""Initialize the OOP metrics analyzer.
		
		Args:
			use_ast: If True, use AST-based parsing (more accurate). If False, use regex.
			repo_path: Optional path to the repository being analyzed to store indices centrally.
		"""
		self.packages = {}  # package_path -> metrics
		self.files = {}     # file_path -> metrics
		self.dependencies = defaultdict(set)  # file_path -> set of dependencies
		self.dependents = defaultdict(set)    # file_path -> set of dependents
		self.use_ast = use_ast  # AST always available
		from src.gitstats_index import CodeSearchIndex, get_db_path_for_repo, find_repo_root
		import os
		import graphqlite
		
		# Resolve database path based on repository path or current directory
		self.repo_path = repo_path if repo_path else find_repo_root(os.getcwd())
		db_path = get_db_path_for_repo(self.repo_path)
		
		self.search_index = CodeSearchIndex(db_path)
		self.graph = graphqlite.Graph(db_path=db_path)

	def analyze_file(self, filepath: str, content: str, file_extension: str) -> Dict:
		"""
		Analyze a single file for OOP metrics.
		
		Args:
			filepath: Path to the file
			content: Content of the file
			file_extension: File extension (e.g., '.py', '.java')
			
		Returns:
			Dictionary containing OOP metrics for the file
		"""
		metrics = {
			'filepath': filepath,
			'extension': file_extension,
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'instability': 0.0,
			'abstractness': 0.0,
			'distance_main_sequence': 0.0,
			'zone': 'unknown',
			'interpretation': 'unknown',
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': [],
			'dependents': [],
			# Function-level metrics (applies to all files)
			'function_count': 0,
			'function_wmc': 0,  # Sum of all function complexities
			'functions': [],
			'classes': [],
			# CK Metrics - aggregated at file level
			'ck_metrics': {
				'wmc_total': 0,
				'avg_wmc': 0.0,
				'max_dit': 0,
				'total_noc': 0,
				'avg_cbo': 0.0,
				'avg_rfc': 0.0,
				'avg_lcom': 0.0,
			}
		}

		if not content.strip():
			return metrics

		try:
			# Try AST-based analysis first (more accurate)
			if self.use_ast:
				ast_metrics = self._analyze_with_ast(content, filepath, file_extension)
				if ast_metrics:
					metrics.update(ast_metrics)
				else:
					# Fall back to regex-based analysis
					metrics.update(self._analyze_with_regex(content, filepath, file_extension))
			else:
				# Use regex-based analysis
				metrics.update(self._analyze_with_regex(content, filepath, file_extension))

			# Calculate derived metrics
			metrics = self._calculate_derived_metrics(metrics)

			# Store for package-level analysis
			self.files[filepath] = metrics

		except Exception as e:
			print(f'Warning: OOP metrics calculation failed for {filepath}: {e}')

		return metrics

	def _analyze_with_ast(self, content: str, filepath: str, file_extension: str) -> Dict:
		"""
		Analyze file using AST-based parsing (more accurate than regex).
		
		Args:
			content: File content
			filepath: Path to the file
			file_extension: File extension
			
		Returns:
			Dictionary with OOP metrics or None if parsing fails
		"""
		if False:  # AST always available in combined module
			return None

		try:
			tree = parse(content, file_extension)
			content_lines = content.splitlines()
			self.search_index.clear_file(filepath)
			# Clear previous graph database nodes for this file
			try:
				self.graph.query("MATCH (f:File {id: $id})-[r1:REL_CONTAINS]->(c:Class)-[r2:REL_CONTAINS]->(m:Method) DETACH DELETE m", {"id": filepath})
				self.graph.query("MATCH (f:File {id: $id})-[r:REL_CONTAINS]->(child) DETACH DELETE child", {"id": filepath})
				self.graph.query("MATCH (f:File {id: $id}) DETACH DELETE f", {"id": filepath})
			except Exception:
				pass

			metrics = {
				'classes_defined': 0,
				'abstract_classes': 0,
				'interfaces_defined': 0,
				'method_count': 0,
				'attribute_count': 0,
				'efferent_coupling': 0,
				'dependencies': [],
				# CK Metrics - aggregated at file level
				'ck_metrics': {
					'wmc_total': 0,
					'avg_wmc': 0.0,
					'max_dit': 0,
					'total_noc': 0,
					'avg_cbo': 0.0,
					'avg_rfc': 0.0,
					'avg_lcom': 0.0,
				},
				# Drill-down: per-class metrics
				'classes': [],
				# Drill-down: per-function metrics
				'functions': []
			}

			# Collect all classes for CK metrics calculation
			all_classes = []
			all_class_names = set()
			inheritance_map = {}

			# First pass: collect class info
			for node in walk(tree):
				if isinstance(node, ClassDef):
					all_classes.append(node)
					all_class_names.add(node.name)
					inheritance_map[node.name] = node.bases
				elif isinstance(node, InterfaceDef):
					all_class_names.add(node.name)

			# Second pass: calculate metrics
			nodes_to_index = []
			for node in walk(tree):
				if isinstance(node, ClassDef):
					metrics['classes_defined'] += 1
					if node.is_abstract:
						metrics['abstract_classes'] += 1
					metrics['method_count'] += len(node.methods)
					metrics['attribute_count'] += len(node.attributes)

					# Apply CK metrics to this class
					apply_ck_metrics_to_class(node, all_classes, inheritance_map, all_class_names)

					# Aggregate CK metrics at file level
					metrics['ck_metrics']['wmc_total'] += node.wmc
					metrics['ck_metrics']['max_dit'] = max(metrics['ck_metrics']['max_dit'], node.dit)
					metrics['ck_metrics']['total_noc'] += node.noc

					# Index class
					body = '\n'.join(content_lines[max(0, node.lineno-1):node.end_lineno]) if node.lineno > 0 else ''
					nodes_to_index.append({
						'name': node.name,
						'node_type': 'class',
						'filepath': filepath,
						'body_text': body,
						'start_line': node.lineno,
						'end_line': node.end_lineno,
						'metrics': {'wmc': node.wmc, 'cbo': node.cbo, 'rfc': node.rfc, 'lcom': node.lcom}
					})

					# Store per-class details for drill-down
					class_info = {
						'name': node.name,
						'lineno': node.lineno,
						'is_abstract': node.is_abstract,
						'bases': node.bases,
						'wmc': node.wmc,
						'dit': node.dit,
						'noc': node.noc,
						'cbo': node.cbo,
						'rfc': node.rfc,
						'lcom': node.lcom,
						'coupled_classes': list(node.coupled_classes),
						'method_count': len(node.methods),
						'attribute_count': len(node.attributes),
						# Per-method details for drill-down
						'methods': [
							{
								'name': m.name,
								'lineno': m.lineno,
								'cyclomatic_complexity': m.cyclomatic_complexity,
								'accessed_attributes': list(m.accessed_attributes),
								'called_methods': list(m.called_methods),
								'is_abstract': m.is_abstract,
								'is_static': m.is_static,
							}
							for m in node.methods
						]
					}
					metrics['classes'].append(class_info)

					for m in node.methods:
						m_body = '\n'.join(content_lines[max(0, m.lineno-1):m.end_lineno]) if m.lineno > 0 else ''
						nodes_to_index.append({
							'name': f"{node.name}.{m.name}",
							'node_type': 'method',
							'filepath': filepath,
							'body_text': m_body,
							'start_line': m.lineno,
							'end_line': m.end_lineno,
							'metrics': {'cyclomatic_complexity': m.cyclomatic_complexity}
						})

				elif isinstance(node, InterfaceDef):
					metrics['interfaces_defined'] += 1
					metrics['abstract_classes'] += 1  # Interfaces are abstract
					metrics['method_count'] += len(node.methods)

				elif isinstance(node, ImportDef):
					if node.module:
						metrics['dependencies'].append(node.module)

			# Calculate averages for CK metrics
			if metrics['classes_defined'] > 0:
				metrics['ck_metrics']['avg_wmc'] = metrics['ck_metrics']['wmc_total'] / metrics['classes_defined']
				total_cbo = sum(c['cbo'] for c in metrics['classes'])
				total_rfc = sum(c['rfc'] for c in metrics['classes'])
				total_lcom = sum(c['lcom'] for c in metrics['classes'])
				metrics['ck_metrics']['avg_cbo'] = total_cbo / metrics['classes_defined']
				metrics['ck_metrics']['avg_rfc'] = total_rfc / metrics['classes_defined']
				metrics['ck_metrics']['avg_lcom'] = total_lcom / metrics['classes_defined']

			# Store standalone function metrics for drill-down
			for func in tree.functions:
				metrics['functions'].append({
					'name': func.name,
					'lineno': func.lineno,
					'cyclomatic_complexity': func.cyclomatic_complexity,
					'accessed_attributes': list(func.accessed_attributes),
					'called_methods': list(func.called_methods),
				})

				f_body = '\n'.join(content_lines[max(0, func.lineno-1):func.end_lineno]) if func.lineno > 0 else ''
				nodes_to_index.append({
					'name': func.name,
					'node_type': 'function',
					'filepath': filepath,
					'body_text': f_body,
					'start_line': func.lineno,
					'end_line': func.end_lineno,
					'metrics': {'cyclomatic_complexity': func.cyclomatic_complexity}
				})

			if nodes_to_index:
				self.search_index.index_nodes(nodes_to_index)

			# Populate graph database nodes and edges for this file
			import os
			graph_nodes = []
			graph_edges = []
			
			file_id = filepath
			graph_nodes.append((file_id, {"name": os.path.basename(filepath), "path": filepath, "extension": file_extension}, "File"))
			
			for node in walk(tree):
				if isinstance(node, ClassDef):
					class_id = f"{filepath}::{node.name}"
					graph_nodes.append((class_id, {"name": node.name}, "Class"))
					graph_edges.append((file_id, class_id, {}, "CONTAINS"))
					
					for m in node.methods:
						method_id = f"{class_id}::{m.name}"
						graph_nodes.append((method_id, {"name": m.name, "complexity": m.cyclomatic_complexity}, "Method"))
						graph_edges.append((class_id, method_id, {}, "CONTAINS"))
						
			for func in tree.functions:
				func_id = f"{filepath}::{func.name}"
				graph_nodes.append((func_id, {"name": func.name, "complexity": func.cyclomatic_complexity}, "Function"))
				graph_edges.append((file_id, func_id, {}, "CONTAINS"))
				
			for dep in metrics['dependencies']:
				# Best-effort dependency file resolution
				resolved_dep_path = None
				parts = dep.split('.')
				dep_suffix = "/".join(parts)
				for ext in ['.py', '.java', '.js', '.ts', '.go', '.rs', '.swift']:
					test_rel_path = dep_suffix + ext
					test_abs_path = os.path.join(self.repo_path, test_rel_path)
					if os.path.exists(test_abs_path):
						resolved_dep_path = os.path.abspath(test_abs_path)
						break
				
				if resolved_dep_path:
					dep_id = resolved_dep_path
					graph_nodes.append((dep_id, {"name": os.path.basename(resolved_dep_path), "path": resolved_dep_path}, "File"))
				else:
					dep_id = dep
					graph_nodes.append((dep_id, {"name": dep}, "Module"))
					
				graph_edges.append((file_id, dep_id, {}, "DEPENDS_ON"))
				
			if graph_nodes:
				try:
					id_map = self.graph.insert_nodes_bulk(graph_nodes)
					if graph_edges:
						self.graph.insert_edges_bulk(graph_edges, id_map)
				except Exception as e:
					print(f"Warning: Failed to insert graph nodes/edges for {filepath}: {e}")

			# Calculate function-level metrics for all files (OOP and non-OOP)
			metrics['function_count'] = len(tree.functions)
			metrics['function_wmc'] = sum(f['cyclomatic_complexity'] for f in metrics['functions'])

			# Deduplicate dependencies
			metrics['dependencies'] = list(set(metrics['dependencies']))
			metrics['efferent_coupling'] = len(metrics['dependencies'])

			# Store dependencies for afferent coupling calculation
			self.dependencies[filepath] = set(metrics['dependencies'])

			return metrics

		except Exception:
			return None

	def _analyze_with_regex(self, content: str, filepath: str, file_extension: str) -> Dict:
		"""
		Analyze file using regex-based parsing (fallback method).
		
		Args:
			content: File content
			filepath: Path to the file
			file_extension: File extension
			
		Returns:
			Dictionary with OOP metrics
		"""
		# Remove comments and strings for accurate analysis
		cleaned_content = self._remove_comments_and_strings(content, file_extension)

		# Language-specific OOP analysis
		if file_extension in ['.java', '.scala', '.kt']:
			return self._analyze_java_oop(cleaned_content, filepath)
		elif file_extension in ['.py', '.pyi']:
			return self._analyze_python_oop(cleaned_content, filepath)
		elif file_extension in ['.cpp', '.cc', '.cxx', '.hpp', '.hxx', '.h']:
			return self._analyze_cpp_oop(cleaned_content, filepath)
		elif file_extension in ['.js', '.ts', '.jsx', '.tsx']:
			return self._analyze_javascript_oop(cleaned_content, filepath)
		elif file_extension in ['.swift']:
			return self._analyze_swift_oop(cleaned_content, filepath)
		elif file_extension in ['.go']:
			return self._analyze_go_oop(cleaned_content, filepath)
		elif file_extension in ['.rs']:
			return self._analyze_rust_oop(cleaned_content, filepath)

		return {}

	def _calculate_derived_metrics(self, metrics: Dict) -> Dict:
		"""
		Calculate derived OOP metrics from base measurements.
		
		Args:
			metrics: Dictionary containing base metrics
			
		Returns:
			Updated metrics dictionary with derived values
		"""
		# Calculate Abstractness: A = Abstract classes / Total classes
		if metrics['classes_defined'] > 0:
			metrics['abstractness'] = metrics['abstract_classes'] / metrics['classes_defined']
		else:
			metrics['abstractness'] = 0.0

		# Calculate Instability: I = Ce / (Ce + Ca)
		ce = metrics['efferent_coupling']
		ca = metrics['afferent_coupling']

		if (ce + ca) > 0:
			metrics['instability'] = ce / (ce + ca)
		else:
			metrics['instability'] = 0.0

		# Calculate Distance from Main Sequence: D = |A + I - 1|
		a = metrics['abstractness']
		i = metrics['instability']
		metrics['distance_main_sequence'] = abs(a + i - 1.0)

		# Add overall coupling metric
		metrics['coupling'] = ce + ca

		# Determine zone and interpretation
		metrics['zone'] = self._determine_zone(a, i, metrics['distance_main_sequence'])
		metrics['interpretation'] = self._interpret_distance(metrics['distance_main_sequence'],
															  a, i, metrics['zone'])

		return metrics

	def _determine_zone(self, abstractness: float, instability: float, distance: float) -> str:
		"""
		Determine which zone the package/file falls into.
		
		Args:
			abstractness: Abstractness metric (A)
			instability: Instability metric (I)
			distance: Distance from main sequence (D)
			
		Returns:
			Zone classification string
		"""
		# Main Sequence: Ideal zone where D is close to 0
		if distance < 0.2:
			return 'main_sequence'

		# Zone of Pain: High stability (low I), low abstraction (low A)
		# A → 0, I → 0, making A + I - 1 → -1, so D → 1
		if abstractness < 0.3 and instability < 0.3:
			return 'zone_of_pain'

		# Zone of Uselessness: High abstraction (high A), low stability (high I)
		# A → 1, I → 1, making A + I - 1 → 1, so D → 1
		if abstractness > 0.7 and instability > 0.7:
			return 'zone_of_uselessness'

		# Transitional zones
		if distance < 0.4:
			return 'near_main_sequence'
		else:
			return 'far_from_main_sequence'

	def _interpret_distance(self, distance: float, abstractness: float,
						   instability: float, zone: str) -> str:
		"""
		Provide interpretation of the distance metric.
		
		Args:
			distance: Distance from main sequence
			abstractness: Abstractness metric
			instability: Instability metric
			zone: Zone classification
			
		Returns:
			Human-readable interpretation
		"""
		interpretations = {
			'main_sequence': 'Good - Well-balanced package design',
			'near_main_sequence': 'Moderate - Package may benefit from minor refactoring',
			'zone_of_pain': 'Poor - Package is too concrete and stable (difficult to extend)',
			'zone_of_uselessness': 'Poor - Package is too abstract and unstable (unused abstractions)',
			'far_from_main_sequence': 'Poor - Package needs significant refactoring'
		}

		return interpretations.get(zone, 'Unknown design state')

	def _remove_comments_and_strings(self, content: str, file_extension: str) -> str:
		"""
		Remove comments and string literals from code.
		
		Args:
			content: Source code content
			file_extension: File extension
			
		Returns:
			Cleaned content without comments and strings
		"""
		if file_extension == '.py':
			# Remove Python comments and strings
			content = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
			content = re.sub(r'""".*?"""', '', content, flags=re.DOTALL)
			content = re.sub(r"'''.*?'''", '', content, flags=re.DOTALL)
			content = re.sub(r'"[^"]*"', '', content)
			content = re.sub(r"'[^']*'", '', content)

		elif file_extension in ['.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.kt',
							   '.cpp', '.c', '.cc', '.cxx', '.h', '.hpp', '.go', '.rs']:
			# Remove C-style comments and strings
			content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
			content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
			content = re.sub(r'"[^"]*"', '', content)
			content = re.sub(r"'[^']*'", '', content)

		return content

	def _analyze_java_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP metrics for Java/Scala/Kotlin files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count classes (including inner classes)
		class_patterns = [
			r'\bclass\s+(\w+)',
			r'\benum\s+(\w+)',
			r'\b@interface\s+(\w+)'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['classes_defined'] += len(matches)

		# Count abstract classes
		abstract_patterns = [
			r'\babstract\s+class\s+(\w+)',
			r'\babstract\s+.*\s+class\s+(\w+)'
		]
		for pattern in abstract_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['abstract_classes'] += len(matches)

		# Count interfaces
		interface_matches = re.findall(r'\binterface\s+(\w+)', content, re.MULTILINE)
		metrics['interfaces_defined'] = len(interface_matches)
		metrics['abstract_classes'] += len(interface_matches)  # Interfaces are abstract

		# Count methods
		method_patterns = [
			r'\b(public|private|protected|static).*\s+\w+\s*\([^)]*\)\s*\{',
			r'\b\w+\s*\([^)]*\)\s*\{'
		]
		for pattern in method_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['method_count'] += len(matches)

		# Count attributes/fields
		field_patterns = [
			r'\b(public|private|protected|static)\s+[\w<>,\[\]]+\s+\w+\s*[=;]'
		]
		for pattern in field_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['attribute_count'] += len(matches)

		# Analyze dependencies (efferent coupling)
		import_matches = re.findall(r'\bimport\s+([\w.]+)', content)
		metrics['dependencies'] = list(set(import_matches))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies for later analysis
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_python_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP metrics for Python files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count classes
		class_matches = re.findall(r'^class\s+(\w+).*:', content, re.MULTILINE)
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
			# More accurate: count classes that inherit from ABC
			abc_classes = re.findall(r'^class\s+\w+\([^)]*ABC[^)]*\):', content, re.MULTILINE)
			# Also count classes with @abstractmethod decorators
			abstractmethod_count = len(re.findall(r'@abstractmethod', content))
			if abc_classes:
				metrics['abstract_classes'] = len(abc_classes)
			elif abstractmethod_count > 0:
				# Has abstract methods but no explicit ABC inheritance detected
				# Conservative: at least 1 abstract class
				metrics['abstract_classes'] = 1
			else:
				metrics['abstract_classes'] = 0
		else:
			metrics['abstract_classes'] = 0

		# Count methods (def within classes)
		method_matches = re.findall(r'^\s+def\s+(\w+)\s*\(.*\):', content, re.MULTILINE)
		metrics['method_count'] = len(method_matches)

		# Count attributes (self.attribute assignments)
		attribute_matches = re.findall(r'self\.(\w+)\s*=', content)
		metrics['attribute_count'] = len(set(attribute_matches))

		# Analyze dependencies (efferent coupling)
		import_patterns = [
			r'^from\s+([\w.]+)\s+import',
			r'^import\s+([\w.]+)'
		]
		dependencies = []
		for pattern in import_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			dependencies.extend(matches)

		metrics['dependencies'] = list(set(dependencies))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies for later analysis
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_cpp_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP metrics for C++ files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count classes and structs
		class_patterns = [
			r'\bclass\s+(\w+)',
			r'\bstruct\s+(\w+)'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content)
			metrics['classes_defined'] += len(matches)

		# Count abstract classes (virtual methods = 0)
		virtual_matches = re.findall(r'virtual\s+.*\s*=\s*0\s*;', content)
		if virtual_matches:
			metrics['abstract_classes'] = 1  # Conservative estimate

		# Count methods
		method_patterns = [
			r'\b\w+\s*\([^)]*\)\s*\{',
			r'\b(public|private|protected):\s*\n\s*\w+\s*\([^)]*\)'
		]
		for pattern in method_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['method_count'] += len(matches)

		# Count attributes (member variables)
		member_patterns = [
			r'\b(public|private|protected):\s*\n\s*[\w<>,\*&\[\]]+\s+\w+\s*;'
		]
		for pattern in member_patterns:
			matches = re.findall(pattern, content, re.MULTILINE)
			metrics['attribute_count'] += len(matches)

		# Analyze dependencies (includes)
		include_matches = re.findall(r'#include\s*[<"]([\w./]+)[>"]', content)
		metrics['dependencies'] = list(set(include_matches))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_javascript_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP metrics for JavaScript/TypeScript files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count classes
		class_matches = re.findall(r'\bclass\s+(\w+)', content)
		metrics['classes_defined'] = len(class_matches)

		# Count interfaces (TypeScript)
		interface_matches = re.findall(r'\binterface\s+(\w+)', content)
		metrics['interfaces_defined'] = len(interface_matches)
		metrics['abstract_classes'] += len(interface_matches)

		# Count abstract classes (TypeScript)
		abstract_matches = re.findall(r'\babstract\s+class\s+(\w+)', content)
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
			r'this\.(\w+)\s*=',
			r'\b\w+:\s*[\w\[\]<>]+\s*[;,]'
		]
		for pattern in property_patterns:
			matches = re.findall(pattern, content)
			metrics['attribute_count'] += len(matches)

		# Analyze dependencies
		import_patterns = [
			r'import\s+.*\s+from\s+["\']+([\w./]+)["\']',
			r'require\s*\(["\']+([\w./]+)["\']'
		]
		dependencies = []
		for pattern in import_patterns:
			matches = re.findall(pattern, content)
			dependencies.extend(matches)

		metrics['dependencies'] = list(set(dependencies))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_swift_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP metrics for Swift files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count classes and structs
		class_patterns = [
			r'\bclass\s+(\w+)',
			r'\bstruct\s+(\w+)'
		]
		for pattern in class_patterns:
			matches = re.findall(pattern, content)
			metrics['classes_defined'] += len(matches)

		# Count protocols (Swift's interfaces)
		protocol_matches = re.findall(r'\bprotocol\s+(\w+)', content)
		metrics['interfaces_defined'] = len(protocol_matches)
		metrics['abstract_classes'] += len(protocol_matches)

		# Count methods/functions
		method_matches = re.findall(r'\bfunc\s+(\w+)\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count properties
		property_patterns = [
			r'\bvar\s+(\w+)\s*:',
			r'\blet\s+(\w+)\s*:'
		]
		for pattern in property_patterns:
			matches = re.findall(pattern, content)
			metrics['attribute_count'] += len(matches)

		# Analyze dependencies
		import_matches = re.findall(r'\bimport\s+(\w+)', content)
		metrics['dependencies'] = list(set(import_matches))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_go_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP-like metrics for Go files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count structs (Go's equivalent to classes)
		struct_matches = re.findall(r'\btype\s+(\w+)\s+struct\s*\{', content)
		metrics['classes_defined'] = len(struct_matches)

		# Count interfaces
		interface_matches = re.findall(r'\btype\s+(\w+)\s+interface\s*\{', content)
		metrics['interfaces_defined'] = len(interface_matches)
		metrics['abstract_classes'] = len(interface_matches)

		# Count methods (functions with receivers)
		method_matches = re.findall(r'\bfunc\s*\([^)]*\)\s*(\w+)\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count struct fields
		field_matches = re.findall(r'^\s*(\w+)\s+[\w\[\]\*]+\s*$', content, re.MULTILINE)
		metrics['attribute_count'] = len(field_matches)

		# Analyze dependencies
		import_matches = re.findall(r'\bimport\s+["\']([\\w/.-]+)["\']', content)
		metrics['dependencies'] = list(set(import_matches))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def _analyze_rust_oop(self, content: str, filepath: str) -> Dict:
		"""Analyze OOP-like metrics for Rust files."""
		metrics = {
			'classes_defined': 0,
			'abstract_classes': 0,
			'interfaces_defined': 0,
			'efferent_coupling': 0,
			'afferent_coupling': 0,
			'method_count': 0,
			'attribute_count': 0,
			'dependencies': []
		}

		# Count structs and enums
		struct_matches = re.findall(r'\bstruct\s+(\w+)', content)
		enum_matches = re.findall(r'\benum\s+(\w+)', content)
		metrics['classes_defined'] = len(struct_matches) + len(enum_matches)

		# Count traits (Rust's interfaces)
		trait_matches = re.findall(r'\btrait\s+(\w+)', content)
		metrics['interfaces_defined'] = len(trait_matches)
		metrics['abstract_classes'] = len(trait_matches)

		# Count impl methods
		method_matches = re.findall(r'\bfn\s+(\w+)\s*\(', content)
		metrics['method_count'] = len(method_matches)

		# Count struct fields
		field_matches = re.findall(r'^\s*(\w+)\s*:\s*[\w<>,\[\]]+\s*,?', content, re.MULTILINE)
		metrics['attribute_count'] = len(field_matches)

		# Analyze dependencies
		use_matches = re.findall(r'\buse\s+([\w:]+)', content)
		extern_matches = re.findall(r'\bextern\s+crate\s+(\w+)', content)
		dependencies = use_matches + extern_matches
		metrics['dependencies'] = list(set(dependencies))
		metrics['efferent_coupling'] = len(metrics['dependencies'])

		# Store dependencies
		self.dependencies[filepath] = set(metrics['dependencies'])

		return metrics

	def calculate_afferent_coupling(self):
		"""
		Calculate afferent coupling (Ca) for all files based on dependency analysis.
		This requires analyzing all files first to understand the dependency graph.
		"""
		# Build reverse dependency map (who depends on whom)
		for filepath, deps in self.dependencies.items():
			for dep in deps:
				# Try to find matching files
				for target_file in self.files.keys():
					# Simple matching: if dependency name is in target file path
					if dep.replace('.', '/') in target_file or dep in target_file:
						self.dependents[target_file].add(filepath)

		# Update afferent coupling counts
		for filepath, metrics in self.files.items():
			metrics['afferent_coupling'] = len(self.dependents.get(filepath, set()))
			metrics['dependents'] = list(self.dependents.get(filepath, set()))

			# Recalculate derived metrics with updated afferent coupling
			self.files[filepath] = self._calculate_derived_metrics(metrics)

	def analyze_package(self, package_path: str) -> Dict:
		"""
		Aggregate OOP metrics at the package level.
		
		Args:
			package_path: Path to the package directory
			
		Returns:
			Dictionary containing aggregated package metrics
		"""
		package_files = [fp for fp in self.files.keys() if fp.startswith(package_path)]

		if not package_files:
			return None

		# Aggregate metrics
		total_classes = sum(self.files[fp]['classes_defined'] for fp in package_files)
		total_abstract = sum(self.files[fp]['abstract_classes'] for fp in package_files)
		total_ce = sum(self.files[fp]['efferent_coupling'] for fp in package_files)
		total_ca = sum(self.files[fp]['afferent_coupling'] for fp in package_files)

		# Calculate package-level metrics
		package_metrics = {
			'package_path': package_path,
			'file_count': len(package_files),
			'total_classes': total_classes,
			'total_abstract_classes': total_abstract,
			'efferent_coupling': total_ce,
			'afferent_coupling': total_ca,
			'abstractness': total_abstract / total_classes if total_classes > 0 else 0.0,
			'instability': total_ce / (total_ce + total_ca) if (total_ce + total_ca) > 0 else 0.0
		}

		# Calculate distance from main sequence
		a = package_metrics['abstractness']
		i = package_metrics['instability']
		package_metrics['distance_main_sequence'] = abs(a + i - 1.0)
		package_metrics['zone'] = self._determine_zone(a, i, package_metrics['distance_main_sequence'])
		package_metrics['interpretation'] = self._interpret_distance(
			package_metrics['distance_main_sequence'], a, i, package_metrics['zone']
		)

		self.packages[package_path] = package_metrics
		return package_metrics

	def get_summary_report(self) -> Dict:
		"""
		Generate a summary report of all analyzed OOP metrics.
		
		Returns:
			Dictionary containing summary statistics
		"""
		if not self.files:
			return {'error': 'No files analyzed'}

		distances = [m['distance_main_sequence'] for m in self.files.values()]
		zones = [m['zone'] for m in self.files.values()]

		report = {
			'total_files_analyzed': len(self.files),
			'average_distance': sum(distances) / len(distances) if distances else 0.0,
			'min_distance': min(distances) if distances else 0.0,
			'max_distance': max(distances) if distances else 0.0,
			'zone_distribution': {
				'main_sequence': zones.count('main_sequence'),
				'near_main_sequence': zones.count('near_main_sequence'),
				'zone_of_pain': zones.count('zone_of_pain'),
				'zone_of_uselessness': zones.count('zone_of_uselessness'),
				'far_from_main_sequence': zones.count('far_from_main_sequence')
			},
			'files_by_zone': {
				'main_sequence': [fp for fp, m in self.files.items() if m['zone'] == 'main_sequence'],
				'zone_of_pain': [fp for fp, m in self.files.items() if m['zone'] == 'zone_of_pain'],
				'zone_of_uselessness': [fp for fp, m in self.files.items() if m['zone'] == 'zone_of_uselessness']
			},
			'recommendations': self._generate_recommendations(distances, zones)
		}

		return report

	def _generate_recommendations(self, distances: List[float], zones: List[str]) -> List[str]:
		"""
		Generate recommendations based on OOP metrics analysis.
		
		Args:
			distances: List of distance values
			zones: List of zone classifications
			
		Returns:
			List of recommendation strings
		"""
		recommendations = []

		avg_distance = sum(distances) / len(distances) if distances else 0.0

		if avg_distance > 0.4:
			recommendations.append(
				"⚠️  Average distance from main sequence is high (> 0.4). "
				"Consider significant refactoring to improve design balance."
			)
		elif avg_distance > 0.2:
			recommendations.append(
				"⚡ Average distance from main sequence is moderate (0.2-0.4). "
				"Some refactoring may improve design quality."
			)
		else:
			recommendations.append(
				"✅ Average distance from main sequence is good (< 0.2). "
				"Package design is well-balanced."
			)

		pain_count = zones.count('zone_of_pain')
		if pain_count > 0:
			recommendations.append(
				f"🔴 {pain_count} file(s) in Zone of Pain (stable but concrete). "
				"Consider adding abstraction layers to improve extensibility."
			)

		useless_count = zones.count('zone_of_uselessness')
		if useless_count > 0:
			recommendations.append(
				f"🟡 {useless_count} file(s) in Zone of Uselessness (abstract but unstable). "
				"Consider adding concrete implementations or removing unused abstractions."
			)

		return recommendations


def format_oop_report(metrics: Dict, verbose: bool = False) -> str:
	"""
	Format OOP metrics into a JSON report.
	
	Args:
		metrics: Dictionary containing OOP metrics
		verbose: Whether to include detailed information
		
	Returns:
		Formatted JSON string
	"""
	import json
	return json.dumps(metrics, indent=2)
