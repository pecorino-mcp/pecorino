import os
from typing import Dict, List, Tuple, Any, Optional

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

            # INHERITS — check superclasses/superclass/base list via fields
            seen_inherits = set()
            for field_name in ('superclasses', 'superclass', 'super_class'):
                superclasses_node = node.child_by_field_name(field_name)
                if superclasses_node:
                    for child in superclasses_node.children:
                        if child.is_named:
                            base_name = self.ts_text(child, source)
                            if base_name and base_name not in seen_inherits:
                                seen_inherits.add(base_name)
                                base_id = self.symbol_table.get(base_name)
                                if not base_id:
                                    ext_id = f"external::{base_name}"
                                    self.ensure_node(ext_id, "External", base_name, base_name, "__external__", 0, 0)
                                    base_id = ext_id
                                self.edges["INHERITS"].append((node_id, base_id, {}))

            # Also check Java-style superclass/interfaces via tree children
            for child in node.children:
                if child.type == 'superclass':
                    for sc in child.children:
                        if sc.is_named:
                            base_name = self.ts_text(sc, source)
                            if base_name and base_name not in seen_inherits:
                                seen_inherits.add(base_name)
                                base_id = self.symbol_table.get(base_name, f"external::{base_name}")
                                if base_id.startswith("external::"):
                                    self.ensure_node(base_id, "External", base_name, base_name, "__external__", 0, 0)
                                self.edges["INHERITS"].append((node_id, base_id, {}))
                elif child.type == 'super_interfaces':
                    for si in child.children:
                        if si.is_named:
                            iface_name = self.ts_text(si, source)
                            if iface_name and iface_name not in seen_inherits:
                                seen_inherits.add(iface_name)
                                iface_id = self.symbol_table.get(iface_name, f"external::{iface_name}")
                                if iface_id.startswith("external::"):
                                    self.ensure_node(iface_id, "External", iface_name, iface_name, "__external__", 0, 0)
                                self.edges["INHERITS"].append((node_id, iface_id, {}))

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

            # RETURNS
            return_type_node = node.child_by_field_name('return_type')
            if return_type_node:
                ret_type = self.ts_text(return_type_node, source)
                type_id = self.symbol_table.get(ret_type)
                if not type_id:
                    ext_id = f"external_type::{ret_type}"
                    self.ensure_node(ext_id, "ExternalType", ret_type, ret_type, "__external__", 0, 0)
                    type_id = ext_id
                self.edges["RETURNS"].append((node_id, type_id, {}))

            # PARAMETER_OF
            parameters_node = node.child_by_field_name('parameters')
            if parameters_node:
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
                                self.ensure_node(param_id, "Parameter", param_name, param_qname, self.file_path,
                                                 child.start_point[0] + 1, child.end_point[0] + 1)
                                self.edges["PARAMETER_OF"].append((param_id, node_id, {"position": pos}))
                                pos += 1

            containers.append((node.start_byte, node.end_byte, node_id, qname))

        # Re-sort containers after adding functions
        containers.sort(key=lambda x: (x[0], -x[1]))

        # Process imports
        for node in import_nodes:
            import_text = self.ts_text(node, source)
            parent_id, _ = self._find_parent(node, containers, file_node_id)
            resolved_path = self.resolve_import_fn(import_text, self.file_path, self.repo_path) if self.resolve_import_fn else None
            if resolved_path:
                target_file_node_id = resolved_path
                is_external = False
                self.ensure_node(target_file_node_id, "File", os.path.basename(resolved_path), resolved_path, resolved_path, 0, 0)
            else:
                target_file_node_id = f"external_file::{import_text}"
                is_external = True
                self.ensure_node(target_file_node_id, "External", import_text, import_text, "__external__", 0, 0)
            self.edges["IMPORTS"].append((parent_id, target_file_node_id, {"is_external": is_external, "import_text": import_text}))

        # Process calls
        for node in call_nodes:
            parent_id, _ = self._find_parent(node, containers, file_node_id)

            # Extract the function/method name from the call
            # Different languages use different field names
            func_node = (node.child_by_field_name('function') or
                         node.child_by_field_name('name') or
                         node.child_by_field_name('method'))
            if func_node:
                raw_call = self.ts_text(func_node, source)
                simple_name = raw_call.split(".")[-1]
                target_id = self.symbol_table.get(simple_name)
                if not target_id:
                    ext_id = f"external::{raw_call}"
                    self.ensure_node(ext_id, "External", raw_call, raw_call, "__external__", 0, 0)
                    target_id = ext_id
                self.edges["CALLS"].append((parent_id, target_id, {"line": node.start_point[0] + 1}))

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
                self.ensure_node(current_id, "Class", name, current_qname, self.file_path, node.start_point[0] + 1, node.end_point[0] + 1, docstring)
                self.symbol_table[name] = current_id
                self.edges["CONTAINS"].append((parent_id, current_id, {}))

                superclasses_node = node.child_by_field_name('superclasses')
                if superclasses_node:
                    for child in superclasses_node.children:
                        if child.is_named:
                            base_name = self.ts_text(child, source)
                            if base_name:
                                base_id = self.symbol_table.get(base_name)
                                if not base_id:
                                    ext_id = f"external::{base_name}"
                                    self.ensure_node(ext_id, "External", base_name, base_name, "__external__", 0, 0)
                                    base_id = ext_id
                                self.edges["INHERITS"].append((current_id, base_id, {}))

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
                self.ensure_node(current_id, kind, name, current_qname, self.file_path, node.start_point[0] + 1, node.end_point[0] + 1, docstring)
                self.symbol_table[name] = current_id
                self.edges["CONTAINS"].append((parent_id, current_id, {}))

                return_type_node = node.child_by_field_name('return_type')
                if return_type_node:
                    ret_type = self.ts_text(return_type_node, source)
                    type_id = self.symbol_table.get(ret_type)
                    if not type_id:
                        ext_id = f"external_type::{ret_type}"
                        self.ensure_node(ext_id, "ExternalType", ret_type, ret_type, "__external__", 0, 0)
                        type_id = ext_id
                    self.edges["RETURNS"].append((current_id, type_id, {}))

                parameters_node = node.child_by_field_name('parameters')
                if parameters_node:
                    pos = 0
                    for child in parameters_node.children:
                        if child.type in ('identifier', 'typed_parameter'):
                            param_name_node = child.child_by_field_name('name') if child.type == 'typed_parameter' else child
                            param_name = self.ts_text(param_name_node, source)
                            if param_name and param_name not in ('self', 'cls'):
                                param_qname = make_qname(current_qname, param_name)
                                param_id = make_id(self.file_path, "Parameter", param_qname, child.start_point[0] + 1)
                                self.ensure_node(param_id, "Parameter", param_name, param_qname, self.file_path, child.start_point[0] + 1, child.end_point[0] + 1)
                                self.edges["PARAMETER_OF"].append((param_id, current_id, {"position": pos}))
                                pos += 1

        elif node.type in ('call_expression', 'method_invocation'):
            func_node = node.child_by_field_name('function') or node.child_by_field_name('name')
            if func_node:
                raw_call = self.ts_text(func_node, source)
                simple_name = raw_call.split(".")[-1]
                target_id = self.symbol_table.get(simple_name)
                if not target_id:
                    ext_id = f"external::{raw_call}"
                    self.ensure_node(ext_id, "External", raw_call, raw_call, "__external__", 0, 0)
                    target_id = ext_id
                self.edges["CALLS"].append((parent_id, target_id, {"line": node.start_point[0] + 1}))

        elif node.type in ('import_statement', 'import_from_statement', 'preproc_include',
                            'import_declaration', 'use_declaration'):
            import_text = self.ts_text(node, source)
            resolved_path = self.resolve_import_fn(import_text, self.file_path, self.repo_path) if self.resolve_import_fn else None
            if resolved_path:
                target_file_node_id = resolved_path
                is_external = False
                self.ensure_node(target_file_node_id, "File", os.path.basename(resolved_path), resolved_path, resolved_path, 0, 0)
            else:
                target_file_node_id = f"external_file::{import_text}"
                is_external = True
                self.ensure_node(target_file_node_id, "External", import_text, import_text, "__external__", 0, 0)
            self.edges["IMPORTS"].append((parent_id, target_file_node_id, {"is_external": is_external, "import_text": import_text}))

        for child in node.children:
            if child.is_named:
                self._walk_fallback(child, source, current_id, current_qname)
