import os
import re
from typing import Any, Dict, List, Optional, Tuple

import tree_sitter

# Cache compiled queries per language to avoid recompilation
_query_cache: Dict[str, tree_sitter.Query] = {}

# Path to the consolidated .scm query files
_QUERIES_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'parsers', 'queries')


def _load_graph_query(language: str, lang_obj) -> Optional[tree_sitter.Query]:
    """Load and compile the .scm query file for a language.

    Returns a compiled tree_sitter.Query, or None if the file doesn't exist
    or fails to compile.
    """
    if language in _query_cache:
        return _query_cache[language]

    query_path = os.path.normpath(os.path.join(_QUERIES_DIR, f'{language}.scm'))
    if not os.path.exists(query_path):
        return None

    try:
        with open(query_path, encoding='utf-8') as f:
            query_str = f.read()
        query = tree_sitter.Query(lang_obj, query_str)
        _query_cache[language] = query
        return query
    except Exception:
        return None


def make_qname(parent_qname: str, name: str) -> str:
    return f"{parent_qname}.{name}" if parent_qname else name


def make_id(file: str, kind: str, qname: str, line: int) -> str:
    return f"{file}::{kind}::{qname}::{line}"


class TreeSitterExtractor:
    def __init__(self, file_path: str, repo_path: str, resolve_import_fn):
        self.file_path = file_path
        self.repo_path = repo_path
        self.resolve_import_fn = resolve_import_fn
        self.symbol_table: Dict[str, str] = {}

        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[str, List[Tuple[str, str, dict]]] = {
            "CONTAINS": [],
            "CALLS": [],
            "IMPORTS": [],
            "INHERITS": [],
            "PARAMETER_OF": [],
            "RETURNS": []
        }

    def ensure_node(self, id: str, kind: str, name: str, qualified_name: str, file: str, line: int, end_line: int, docstring: str = "", start_byte: int = 0, end_byte: int = 0):
        if id not in self.nodes:
            self.nodes[id] = {
                "id": id,
                "kind": kind,
                "name": name,
                "qualified_name": qualified_name,
                "file": file,
                "line": line,
                "end_line": end_line,
                "docstring": docstring,
                "start_byte": start_byte,
                "end_byte": end_byte
            }
        elif docstring:
            self.nodes[id]["docstring"] = docstring
        return id

    def ts_text(self, node, source: bytes) -> str:
        if not node:
            return ""
        return source[node.start_byte:node.end_byte].decode('utf-8', errors='ignore').strip()

    def extract_docstring(self, node, source: bytes) -> str:
        body = node.child_by_field_name('body')
        if body and body.type == 'block' and len(body.children) > 0:
            first_stmt = body.children[0]
            if first_stmt.type == 'expression_statement' and len(first_stmt.children) > 0:
                first_expr = first_stmt.children[0]
                if first_expr.type == 'string':
                    doc = self.ts_text(first_expr, source)
                    if doc.startswith('"""') and doc.endswith('"""'): return doc[3:-3].strip()
                    if doc.startswith("'''") and doc.endswith("'''"): return doc[3:-3].strip()
                    if doc.startswith('"') and doc.endswith('"'): return doc[1:-1].strip()
                    if doc.startswith("'") and doc.endswith("'"): return doc[1:-1].strip()
                    return doc.strip()
        return ""

    # ── Shared extraction helpers ───────────────────────────────

    def _extract_inheritance(self, node, node_id: str, source: bytes):
        """Extract INHERITS edges for a class/interface node."""
        seen_inherits = set()

        def _add_base(base_name: str):
            if not base_name or base_name in seen_inherits:
                return
            seen_inherits.add(base_name)
            base_id = self.symbol_table.get(base_name)
            if not base_id:
                base_id = f"external::{base_name}"
                self.ensure_node(base_id, "External", base_name, base_name, "__external__", 0, 0)
            self.edges["INHERITS"].append((node_id, base_id, {}))

        # Check superclasses/superclass/base list via fields
        for field_name in ('superclasses', 'superclass', 'super_class'):
            superclasses_node = node.child_by_field_name(field_name)
            if superclasses_node:
                for child in superclasses_node.children:
                    if child.is_named:
                        _add_base(self.ts_text(child, source))

        # Also check Java-style superclass/interfaces via tree children
        for child in node.children:
            if child.type in ('superclass', 'super_interfaces'):
                for item in child.children:
                    if item.is_named:
                        _add_base(self.ts_text(item, source))

    def _extract_return_type(self, node, node_id: str, source: bytes):
        """Extract RETURNS edge for a function/method node."""
        return_type_node = node.child_by_field_name('return_type')
        if return_type_node:
            ret_type = self.ts_text(return_type_node, source)
            if ret_type:
                type_id = self.symbol_table.get(ret_type)
                if not type_id:
                    type_id = f"external_type::{ret_type}"
                    self.ensure_node(type_id, "ExternalType", ret_type, ret_type, "__external__", 0, 0)
                self.edges["RETURNS"].append((node_id, type_id, {}))

    def _extract_parameters(self, node, node_id: str, qname: str, source: bytes):
        """Extract Parameter nodes and PARAMETER_OF edges for a function/method node."""
        parameters_node = node.child_by_field_name('parameters')
        if not parameters_node:
            return
        pos = 0
        for child in parameters_node.children:
            if child.type in ('identifier', 'typed_parameter', 'formal_parameter',
                              'required_parameter', 'optional_parameter',
                              'spread_parameter', 'parameter'):
                param_name_node = child.child_by_field_name('name') if child.type != 'identifier' else child
                if param_name_node:
                    param_name = self.ts_text(param_name_node, source)
                    if param_name and param_name not in ('self', 'cls', 'this'):
                        param_qname = make_qname(qname, param_name)
                        param_id = make_id(self.file_path, "Parameter", param_qname, child.start_point[0] + 1)
                        self.ensure_node(
                            param_id,
                            "Parameter",
                            param_name,
                            param_qname,
                            self.file_path,
                            child.start_point[0] + 1,
                            child.end_point[0] + 1,
                        )
                        self.edges["PARAMETER_OF"].append((param_id, node_id, {"position": pos}))
                        pos += 1

    def _extract_call(self, node, parent_id: str, source: bytes):
        """Extract CALLS edge from a call expression node."""
        func_node = (
            node.child_by_field_name('function')
            or node.child_by_field_name('name')
            or node.child_by_field_name('method')
        )
        if func_node:
            raw_call = self.ts_text(func_node, source)
            if raw_call:
                simple_name = raw_call.split(".")[-1]
                target_id = self.symbol_table.get(simple_name)
                if not target_id:
                    target_id = f"external::{raw_call}"
                    self.ensure_node(target_id, "External", raw_call, raw_call, "__external__", 0, 0)
                self.edges["CALLS"].append((parent_id, target_id, {"line": node.start_point[0] + 1}))

    def _clean_import_target(self, node, import_text: str, source: bytes) -> str:
        """Extract the clean module/file specifier from an import statement."""
        # 1. Try AST field lookups first
        if node is not None:
            if node.type == 'import_from_statement':
                mod_node = node.child_by_field_name('module_name')
                if mod_node:
                    return self.ts_text(mod_node, source).strip()
            elif node.type == 'preproc_include':
                path_node = node.child_by_field_name('path')
                if path_node:
                    return self.ts_text(path_node, source).strip('"\'<> ')
            elif node.type == 'import_statement':
                src_node = node.child_by_field_name('source')
                if src_node:
                    return self.ts_text(src_node, source).strip('"\'` ')
                name_node = node.child_by_field_name('name')
                if name_node:
                    inner = name_node.child_by_field_name('name') if name_node.type == 'aliased_import' else name_node
                    return self.ts_text(inner, source).strip()

        # 2. Text-based fallback parsing
        text = import_text.strip().rstrip(';').strip()
        if text.startswith('#include'):
            m = re.search(r'["<]([^">]+)[">]', text)
            if m:
                return m.group(1).strip()
            return text[len('#include'):].strip('"\'<> ')

        if text.startswith('from ') and ' import ' in text:
            before_import, after_import = text[5:].split(' import ', 1)
            mod = before_import.strip()
            if mod and set(mod) == {'.'}:
                first_sym = after_import.split(',')[0].split(' as ')[0].strip('() ')
                return f"{mod}{first_sym}" if first_sym else mod
            return mod

        if ' from ' in text and text.startswith('import '):
            after_from = text.rsplit(' from ', 1)[1].strip()
            return after_from.strip('"\'` ')

        if text.startswith('import '):
            rest = text[len('import '):].strip()
            if rest.startswith('static '):
                rest = rest[len('static '):].strip()
            rest = rest.split(',')[0].split(' as ')[0].strip()
            m = re.search(r'["\']([^"\']+)["\']', rest)
            if m:
                return m.group(1).strip()
            return rest.strip('"\'`() ')

        if text.startswith('use '):
            rest = text[len('use '):].strip()
            rest = rest.split('::{')[0].split(' as ')[0].strip()
            return rest

        return text.strip('"\'<> ')

    def _extract_import(self, node, parent_id: str, source: bytes):
        """Extract IMPORTS edge and target File/External node."""
        import_text = self.ts_text(node, source)
        module_target = self._clean_import_target(node, import_text, source)
        resolved_path = (
            self.resolve_import_fn(module_target, self.file_path, self.repo_path)
            if self.resolve_import_fn and module_target
            else None
        )
        if resolved_path and os.path.exists(resolved_path):
            target_file_node_id = resolved_path
            is_external = False
            self.ensure_node(
                target_file_node_id,
                "File",
                os.path.basename(resolved_path),
                resolved_path,
                resolved_path,
                0,
                0,
            )
        else:
            target_file_node_id = f"external_file::{import_text}"
            is_external = True
            self.ensure_node(
                target_file_node_id,
                "External",
                import_text,
                import_text,
                "__external__",
                0,
                0,
            )
        self.edges["IMPORTS"].append(
            (parent_id, target_file_node_id, {"is_external": is_external, "import_text": import_text})
        )

    def extract(self, tree, source: bytes):
        file_node_id = self.file_path
        self.ensure_node(
            id=file_node_id,
            kind="File",
            name=os.path.basename(self.file_path),
            qualified_name=self.file_path,
            file=self.file_path,
            line=1,
            end_line=tree.root_node.end_point[0] + 1 if tree.root_node else 1
        )

        if tree and tree.root_node:
            # Determine language from file extension
            ext = os.path.splitext(self.file_path)[1]
            from src.parsers.tree_sitter_parser import get_language_from_extension, get_language_obj
            language = get_language_from_extension(ext)
            lang_obj = get_language_obj(ext)

            if lang_obj:
                query = _load_graph_query(language, lang_obj)
            else:
                query = None

            if query:
                self._extract_with_query(tree.root_node, source, query, file_node_id)
            else:
                # Fallback: recursive walk for unsupported languages
                self._walk_fallback(tree.root_node, source, parent_id=file_node_id, parent_qname="")

        return self.nodes, self.edges

    # ── Query-driven extraction ─────────────────────────────────

    def _extract_with_query(self, root_node, source: bytes, query: tree_sitter.Query, file_node_id: str):
        """Extract graph nodes/edges using a compiled tree-sitter query.

        Runs the query in a single pass, then processes captures by their
        @graph.* capture name.
        """
        cursor = tree_sitter.QueryCursor(query)
        captures = cursor.captures(root_node)

        # First pass: process classes and functions to build the symbol table
        # and containment hierarchy.
        class_nodes = captures.get("graph.class", [])
        interface_nodes = captures.get("graph.interface", [])
        function_nodes = captures.get("graph.function", [])
        import_nodes = captures.get("graph.import", [])
        call_nodes = captures.get("graph.call", [])
        impl_nodes = captures.get("graph.impl", [])

        # Build a sorted list of (start_byte, end_byte, node_id) for
        # determining the parent of each node
        containers = []  # (start_byte, end_byte, node_id, qname)

        # Process classes
        for node in class_nodes + interface_nodes:
            name_node = node.child_by_field_name('name')
            if not name_node:
                # Fallback: look for identifier child
                for child in node.children:
                    if child.type in ('identifier', 'type_identifier'):
                        name_node = child
                        break
            if not name_node:
                continue
            name = self.ts_text(name_node, source)
            if not name:
                continue

            parent_id, parent_qname = self._find_parent(node, containers, file_node_id)
            qname = make_qname(parent_qname, name)
            kind = "Class"
            node_id = make_id(self.file_path, kind, qname, node.start_point[0] + 1)
            docstring = self.extract_docstring(node, source)
            self.ensure_node(node_id, kind, name, qname, self.file_path,
                             node.start_point[0] + 1, node.end_point[0] + 1, docstring,
                             start_byte=node.start_byte, end_byte=node.end_byte)
            self.symbol_table[name] = node_id
            self.edges["CONTAINS"].append((parent_id, node_id, {}))

            self._extract_inheritance(node, node_id, source)
            containers.append((node.start_byte, node.end_byte, node_id, qname))

        # Process impl blocks (Rust)
        for node in impl_nodes:
            type_node = node.child_by_field_name('type')
            if type_node:
                impl_name = self.ts_text(type_node, source)
                if impl_name:
                    qname = make_qname("", impl_name)
                    node_id = make_id(self.file_path, "Class", qname, node.start_point[0] + 1)
                    self.ensure_node(node_id, "Class", impl_name, qname, self.file_path,
                                     node.start_point[0] + 1, node.end_point[0] + 1,
                                     start_byte=node.start_byte, end_byte=node.end_byte)
                    self.symbol_table[impl_name] = node_id
                    self.edges["CONTAINS"].append((file_node_id, node_id, {}))
                    containers.append((node.start_byte, node.end_byte, node_id, qname))

        # Sort containers by byte range for parent lookup
        containers.sort(key=lambda x: (x[0], -x[1]))

        # Process functions/methods
        for node in function_nodes:
            name_node = node.child_by_field_name('name')
            if not name_node:
                for child in node.children:
                    if child.type == 'identifier':
                        name_node = child
                        break
            # Arrow functions: name lives on the parent variable_declarator
            # e.g. `const foo = () => {}` → parent is variable_declarator with name='foo'
            if not name_node and node.type == 'arrow_function' and node.parent:
                p = node.parent
                if p.type == 'variable_declarator':
                    name_node = p.child_by_field_name('name')
                elif p.type == 'pair':
                    # Object method shorthand: { foo: () => {} }
                    name_node = p.child_by_field_name('key')
                elif p.type == 'assignment_expression':
                    name_node = p.child_by_field_name('left')
            if not name_node:
                continue
            name = self.ts_text(name_node, source)
            if not name:
                continue

            parent_id, parent_qname = self._find_parent(node, containers, file_node_id)
            qname = make_qname(parent_qname, name)
            # Determine Method vs Function:
            # - method_declaration / method_definition nodes are always methods
            # - Go: has a 'receiver' field for methods declared outside classes
            # - fallback: is the parent a Class container?
            if node.type in ('method_declaration', 'method_definition'):
                kind = "Method"
            elif node.child_by_field_name('receiver'):
                kind = "Method"
            elif "Class" in parent_id:
                kind = "Method"
            else:
                kind = "Function"
            node_id = make_id(self.file_path, kind, qname, node.start_point[0] + 1)
            docstring = self.extract_docstring(node, source)
            self.ensure_node(node_id, kind, name, qname, self.file_path,
                             node.start_point[0] + 1, node.end_point[0] + 1, docstring,
                             start_byte=node.start_byte, end_byte=node.end_byte)
            self.symbol_table[name] = node_id
            self.edges["CONTAINS"].append((parent_id, node_id, {}))

            self._extract_return_type(node, node_id, source)
            self._extract_parameters(node, node_id, qname, source)

            containers.append((node.start_byte, node.end_byte, node_id, qname))

        # Re-sort containers after adding functions
        containers.sort(key=lambda x: (x[0], -x[1]))

        # Process imports
        for node in import_nodes:
            parent_id, _ = self._find_parent(node, containers, file_node_id)
            self._extract_import(node, parent_id, source)

        # Process calls
        for node in call_nodes:
            parent_id, _ = self._find_parent(node, containers, file_node_id)
            self._extract_call(node, parent_id, source)

    def _find_parent(self, node, containers, file_node_id: str) -> Tuple[str, str]:
        """Find the innermost container that encloses this node."""
        best_id = file_node_id
        best_qname = ""
        best_span = float('inf')

        for start_byte, end_byte, container_id, container_qname in containers:
            if start_byte <= node.start_byte and node.end_byte <= end_byte:
                span = end_byte - start_byte
                if span < best_span:
                    best_span = span
                    best_id = container_id
                    best_qname = container_qname

        return best_id, best_qname

    # ── Fallback: recursive walk (for unsupported languages) ────

    def _walk_fallback(self, node, source: bytes, parent_id: str, parent_qname: str):
        """Recursive walk — used only when no .scm query file exists."""
        current_id = parent_id
        current_qname = parent_qname

        if node.type in ('class_definition', 'class_specifier', 'struct_specifier',
                          'class_declaration', 'interface_declaration',
                          'struct_item', 'trait_item'):
            name_node = node.child_by_field_name('name')
            if name_node:
                name = self.ts_text(name_node, source)
                current_qname = make_qname(parent_qname, name)
                current_id = make_id(self.file_path, "Class", current_qname, node.start_point[0] + 1)
                docstring = self.extract_docstring(node, source)
                self.ensure_node(
                    current_id,
                    "Class",
                    name,
                    current_qname,
                    self.file_path,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    docstring,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                )
                self.symbol_table[name] = current_id
                self.edges["CONTAINS"].append((parent_id, current_id, {}))
                self._extract_inheritance(node, current_id, source)

        elif node.type in ('function_definition', 'method_declaration',
                            'constructor_declaration', 'function_item',
                            'method_definition'):
            name_node = node.child_by_field_name('name')
            if name_node:
                name = self.ts_text(name_node, source)
                current_qname = make_qname(parent_qname, name)
                kind = "Method" if "Class" in parent_id else "Function"
                current_id = make_id(self.file_path, kind, current_qname, node.start_point[0] + 1)
                docstring = self.extract_docstring(node, source)
                self.ensure_node(
                    current_id,
                    kind,
                    name,
                    current_qname,
                    self.file_path,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    docstring,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                )
                self.symbol_table[name] = current_id
                self.edges["CONTAINS"].append((parent_id, current_id, {}))

                self._extract_return_type(node, current_id, source)
                self._extract_parameters(node, current_id, current_qname, source)

        elif node.type in ('call_expression', 'method_invocation'):
            self._extract_call(node, parent_id, source)

        elif node.type in ('import_statement', 'import_from_statement', 'preproc_include',
                            'import_declaration', 'use_declaration'):
            self._extract_import(node, parent_id, source)

        for child in node.children:
            if child.is_named:
                self._walk_fallback(child, source, current_id, current_qname)
