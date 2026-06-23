"""
GitStats Tree-sitter AST Parser

This module uses tree-sitter to parse source files for multiple languages,
translating the syntax trees into our standard OOP AST node types:
ImportDef, ClassDef, InterfaceDef, FunctionDef, AttributeDef.

It extracts method-level details like cyclomatic complexity, called methods,
accessed attributes, and parameter types.
"""

from collections import defaultdict
from typing import Any, List, Optional, Set

import tree_sitter


# Lazy imports from gitstats_oopmetrics to avoid circular imports
def get_ast_classes():
    from src.parsers.ast import (
        AttributeDef,
        ClassDef,
        FunctionDef,
        ImportDef,
        InterfaceDef,
        ModuleDef,
    )
    return ModuleDef, ClassDef, InterfaceDef, FunctionDef, ImportDef, AttributeDef

def get_language_from_extension(extension: str) -> str:
    """Map file extension to language name."""
    mapping = {
        '.py': 'python', '.pyi': 'python',
        '.java': 'java', '.scala': 'java', '.kt': 'java',
        '.js': 'javascript', '.jsx': 'javascript',
        '.ts': 'typescript', '.tsx': 'typescript',
        '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp',
        '.c': 'cpp', '.h': 'cpp', '.hpp': 'cpp', '.hxx': 'cpp',
        '.go': 'go',
        '.rs': 'rust',
        '.swift': 'swift',
        '.rb': 'ruby'
    }
    return mapping.get(extension.lower(), 'unknown')


class TreeSitterExtractor:
    """Extracts OOP AST nodes from a tree-sitter parse tree."""

    def __init__(self, source: bytes, language: str):
        self.source = source
        self.language = language

        # Resolve AST classes lazily
        self.ModuleDef, self.ClassDef, self.InterfaceDef, self.FunctionDef, self.ImportDef, self.AttributeDef = get_ast_classes()

        self.module = self.ModuleDef()
        self.class_stack = []      # List of ClassDef (for nested classes)
        self.interface_stack = []  # List of InterfaceDef

        # Out-of-line method definitions (for Go receivers & Rust impl blocks)
        self.out_of_line_methods = defaultdict(list)

    def _get_text(self, node) -> str:
        if not node:
            return ""
        return self.source[node.start_byte:node.end_byte].decode('utf-8', errors='ignore').strip()

    def _find_child_by_type(self, node, type_name) -> Optional[Any]:
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _find_children_by_type(self, node, type_name) -> List[Any]:
        return [c for c in node.children if c.type == type_name]

    def _extract_types(self, node) -> List[str]:
        if not node:
            return []
        types = []

        # Compound identifier/type types that we want as a single string
        COMPOUND_TYPES = ('member_expression', 'qualified_identifier', 'nested_identifier', 'attribute')
        # Leaf identifier/type types
        LEAF_TYPES = ('type_identifier', 'user_type', 'identifier')
        # Container/wrapper types where we only want to extract the base name/value
        BASE_ONLY_TYPES = ('subscript', 'generic_type', 'template_type')
        # Types that represent generic parameters/arguments that we want to skip completely
        SKIP_TYPES = ('type_arguments', 'type_parameters', 'type_parameter_list', 'template_argument_list', 'keyword_argument')

        def collect(n):
            if n.type in SKIP_TYPES:
                return

            if n.type in COMPOUND_TYPES:
                types.append(self._get_text(n))
                return

            if n.type in BASE_ONLY_TYPES:
                # Only collect from the value/name part of the generic/subscript/template
                value_node = n.child_by_field_name('value') or n.child_by_field_name('name')
                if not value_node and n.children:
                    value_node = n.children[0]
                if value_node:
                    collect(value_node)
                return

            if n.type in LEAF_TYPES:
                # Check if it has any child nodes that are also identifiers or compound types
                has_child_identifier = any(
                    c.type in LEAF_TYPES or c.type in COMPOUND_TYPES or c.type in BASE_ONLY_TYPES
                    for c in n.children
                )
                if not has_child_identifier:
                    types.append(self._get_text(n))
                    return

            for child in n.children:
                collect(child)

        collect(node)
        return types

    def _parse_class(self, node) -> Any:
        name = ""
        bases = []
        is_abstract = False
        is_interface = False

        # Extract Name
        name_node = node.child_by_field_name('name')
        if name_node:
            name = self._get_text(name_node)
        else:
            # Fallback to search identifier child
            for child in node.children:
                if child.type in ('identifier', 'type_identifier'):
                    name = self._get_text(child)
                    break

        if not name:
            name = "Anonymous"

        # Extract Inheritance/Bases
        if self.language == 'python':
            arg_list = node.child_by_field_name('superclasses')
            if arg_list:
                bases.extend(self._extract_types(arg_list))
        elif self.language == 'java':
            superclass = node.child_by_field_name('superclass')
            if superclass:
                bases.extend(self._extract_types(superclass))
            interfaces = node.child_by_field_name('interfaces')
            if interfaces:
                bases.extend(self._extract_types(interfaces))
            mods = self._find_child_by_type(node, 'modifiers')
            if mods and 'abstract' in self._get_text(mods):
                is_abstract = True
        elif self.language in ('javascript', 'typescript'):
            heritage = self._find_child_by_type(node, 'class_heritage')
            if heritage:
                bases.extend(self._extract_types(heritage))
        elif self.language == 'cpp':
            base_clause = self._find_child_by_type(node, 'base_class_clause')
            if base_clause:
                bases.extend(self._extract_types(base_clause))

        return self.ClassDef(
            name=name,
            bases=bases,
            is_abstract=is_abstract,
            is_interface=is_interface,
            lineno=node.start_point[0] + 1,
            col_offset=node.start_point[1],
            end_lineno=node.end_point[0] + 1,
            end_col_offset=node.end_point[1]
        )

    def _parse_interface(self, node) -> Any:
        name = ""
        extends = []

        name_node = node.child_by_field_name('name')
        if name_node:
            name = self._get_text(name_node)
        else:
            for child in node.children:
                if child.type in ('identifier', 'type_identifier'):
                    name = self._get_text(child)
                    break
        if not name:
            name = "AnonymousInterface"

        if self.language == 'java':
            extends_node = node.child_by_field_name('extends')
            if extends_node:
                extends.extend(self._extract_types(extends_node))
        elif self.language == 'typescript':
            heritage = self._find_child_by_type(node, 'interface_heritage')
            if heritage:
                extends.extend(self._extract_types(heritage))

        return self.InterfaceDef(
            name=name,
            extends=extends,
            lineno=node.start_point[0] + 1,
            col_offset=node.start_point[1]
        )

    def _parse_function(self, node) -> Any:
        name = ""
        args = []
        is_abstract = False
        is_static = False
        visibility = "public"
        return_type = None
        parameter_types = []

        name_node = node.child_by_field_name('name')
        if not name_node:
            decl_node = node.child_by_field_name('declarator')
            while decl_node and decl_node.type in ('function_declarator', 'pointer_declarator', 'reference_declarator'):
                inner_decl = decl_node.child_by_field_name('declarator')
                if inner_decl:
                    decl_node = inner_decl
                else:
                    break
            if decl_node:
                name_node = decl_node

        if name_node:
            name = self._get_text(name_node)
        else:
            for child in node.children:
                if child.type in ('identifier', 'field_identifier', 'qualified_identifier'):
                    name = self._get_text(child)
                    break
        if not name:
            name = "anonymous"

        if self.language == 'java':
            mods = self._find_child_by_type(node, 'modifiers')
            if mods:
                mod_text = self._get_text(mods)
                if 'private' in mod_text:
                    visibility = 'private'
                elif 'protected' in mod_text:
                    visibility = 'protected'
                if 'abstract' in mod_text:
                    is_abstract = True
                if 'static' in mod_text:
                    is_static = True
            return_type_node = node.child_by_field_name('type')
            if return_type_node:
                return_type = self._get_text(return_type_node)
        elif self.language == 'cpp':
            type_node = node.child_by_field_name('type')
            if type_node:
                return_type = self._get_text(type_node)

        # Parameters
        params_node = node.child_by_field_name('parameters')
        if params_node:
            for child in params_node.children:
                if child.type in ('formal_parameter', 'parameter_declaration', 'parameter', 'identifier', 'typed_parameter'):
                    p_name = ""
                    p_type = None

                    if child.type == 'identifier':
                        p_name = self._get_text(child)
                    elif child.type == 'typed_parameter':
                        p_name_node = child.child_by_field_name('name')
                        p_type_node = child.child_by_field_name('type')
                        p_name = self._get_text(p_name_node)
                        p_type = self._get_text(p_type_node)
                    elif self.language == 'java':
                        p_type_node = child.child_by_field_name('type')
                        p_name_node = child.child_by_field_name('declarator')
                        p_name = self._get_text(p_name_node)
                        p_type = self._get_text(p_type_node)
                    elif self.language == 'cpp':
                        p_name_node = child.child_by_field_name('declarator')
                        p_type_node = child.child_by_field_name('type')
                        p_name = self._get_text(p_name_node)
                        p_type = self._get_text(p_type_node)
                    else:
                        p_name = self._get_text(child)

                    if p_name:
                        args.append(p_name)
                    if p_type:
                        parameter_types.append(p_type)

        complexity = 1 + self._count_complexity(node)
        called_methods = self._find_called_methods(node)
        accessed_attributes = self._find_accessed_attributes(node)

        return self.FunctionDef(
            name=name,
            args=args,
            return_type=return_type,
            is_abstract=is_abstract,
            is_static=is_static,
            visibility=visibility,
            body_start=node.start_point[0] + 1,
            body_end=node.end_point[0] + 1,
            cyclomatic_complexity=complexity,
            called_methods=called_methods,
            accessed_attributes=accessed_attributes,
            parameter_types=parameter_types,
            lineno=node.start_point[0] + 1,
            col_offset=node.start_point[1],
            end_lineno=node.end_point[0] + 1,
            end_col_offset=node.end_point[1]
        )

    def _count_complexity(self, node) -> int:
        complexity = 0
        COMPLEXITY_NODES = {
            # Statements
            'if_statement', 'while_statement', 'for_statement', 'for_in_statement',
            'enhanced_for_statement', 'do_statement', 'repeat_while_statement',
            'switch_statement', 'switch_case', 'case_statement', 'default_case',
            'match_pattern', 'alternative', 'guard_statement',
            'select_statement', 'communication_case', 'expression_case',

            # Expressions (Rust, Ruby, JS, Swift)
            'if_expression', 'for_expression', 'while_expression', 'loop_expression',
            'match_expression', 'match_arm', 'ternary_expression',
            'conditional_expression',

            # Ruby / Python constructs
            'if', 'unless', 'while', 'until', 'case', 'when', 'except_clause',
            'except_handler', 'catch_clause'
        }

        if node.type in COMPLEXITY_NODES:
            if len(node.children) > 0 or node.type not in ('if', 'while', 'for', 'unless', 'until', 'case', 'when', 'catch', 'except'):
                complexity += 1
        elif node.type == 'binary_expression':
            op_node = node.child_by_field_name('operator')
            if op_node and op_node.type in ('&&', '||', 'and', 'or'):
                complexity += 1

        for child in node.children:
            complexity += self._count_complexity(child)

        return complexity

    def _find_called_methods(self, node) -> Set[str]:
        called = set()

        def visit(n):
            if n.type in ('call_expression', 'method_invocation', 'call'):
                func_node = n.child_by_field_name('function') or n.child_by_field_name('name')
                if not func_node and n.children:
                    func_node = n.children[0]

                if func_node:
                    if func_node.type in ('member_expression', 'attribute', 'selector_expression', 'field_expression', 'navigation_expression', 'field_access'):
                        obj_node = func_node.child_by_field_name('object') or func_node.child_by_field_name('argument') or func_node.child_by_field_name('operand')
                        prop_node = func_node.child_by_field_name('property') or func_node.child_by_field_name('field') or func_node.child_by_field_name('attribute')

                        if not obj_node and len(func_node.children) >= 2:
                            obj_node = func_node.children[0]
                            prop_node = func_node.children[-1]

                        if obj_node and prop_node:
                            obj_text = self._get_text(obj_node)
                            prop_text = self._get_text(prop_node)
                            if obj_text in ('self', 'this'):
                                called.add(prop_text)
                            else:
                                called.add(f"{obj_text}.{prop_text}")
                    else:
                        called.add(self._get_text(func_node))

            for child in n.children:
                visit(child)

        body_node = node.child_by_field_name('body')
        if body_node:
            visit(body_node)
        else:
            for child in node.children:
                if child.type not in ('identifier', 'type_identifier', 'formal_parameters', 'parameters', 'modifiers'):
                    visit(child)

        return called

    def _find_accessed_attributes(self, node) -> Set[str]:
        attrs = set()

        def visit(n):
            if n.type in ('member_expression', 'attribute', 'field_expression', 'selector_expression', 'field_access'):
                obj_node = n.child_by_field_name('object') or n.child_by_field_name('argument') or n.child_by_field_name('operand')
                prop_node = n.child_by_field_name('property') or n.child_by_field_name('field') or n.child_by_field_name('attribute')

                if not obj_node and len(n.children) >= 2:
                    obj_node = n.children[0]
                    prop_node = n.children[-1]

                if obj_node and prop_node:
                    obj_text = self._get_text(obj_node)
                    prop_text = self._get_text(prop_node)
                    if obj_text in ('self', 'this'):
                        attrs.add(prop_text)

            for child in n.children:
                visit(child)

        body_node = node.child_by_field_name('body')
        if body_node:
            visit(body_node)
        else:
            for child in node.children:
                if child.type not in ('identifier', 'type_identifier', 'formal_parameters', 'parameters', 'modifiers'):
                    visit(child)

        return attrs

    def _parse_import(self, node) -> List[Any]:
        imports = []
        if self.language == 'python':
            if node.type == 'import_statement':
                for child in node.children:
                    if child.type in ('dotted_name', 'aliased_name'):
                        imports.append(self.ImportDef(
                            module=self._get_text(child),
                            lineno=node.start_point[0] + 1
                        ))
            elif node.type == 'import_from_statement':
                module_node = node.child_by_field_name('module_name')
                module = self._get_text(module_node) if module_node else ""
                names = []
                name_list = node.child_by_field_name('name') or node.child_by_field_name('names')
                if name_list:
                    for child in name_list.children:
                        if child.type in ('dotted_name', 'aliased_name', 'identifier'):
                            names.append(self._get_text(child))
                else:
                    for child in node.children:
                        if child.type in ('dotted_name', 'aliased_name', 'identifier') and child != module_node:
                            names.append(self._get_text(child))
                imports.append(self.ImportDef(
                    module=module,
                    names=names,
                    is_from=True,
                    lineno=node.start_point[0] + 1
                ))
        elif self.language == 'java':
            text = self._get_text(node)
            if text.startswith('import '):
                module = text[7:].rstrip(';').strip()
                name = module.split('.')[-1]
                imports.append(self.ImportDef(
                    module=module,
                    names=[name],
                    lineno=node.start_point[0] + 1
                ))
        elif self.language in ('javascript', 'typescript'):
            source = node.child_by_field_name('source')
            if source:
                module = self._get_text(source).strip("'\"")
                imports.append(self.ImportDef(
                    module=module,
                    lineno=node.start_point[0] + 1
                ))
        elif self.language == 'cpp':
            path_node = node.child_by_field_name('path')
            if path_node:
                module = self._get_text(path_node).strip('<>"')
                imports.append(self.ImportDef(
                    module=module,
                    lineno=node.start_point[0] + 1
                ))
        elif self.language == 'go':
            for spec in self._find_children_by_type(node, 'import_spec'):
                path = spec.child_by_field_name('path')
                if path:
                    imports.append(self.ImportDef(
                        module=self._get_text(path).strip('"'),
                        lineno=node.start_point[0] + 1
                    ))
        elif self.language == 'rust':
            text = self._get_text(node)
            if text.startswith('use '):
                module = text[4:].rstrip(';').strip()
                imports.append(self.ImportDef(
                    module=module,
                    lineno=node.start_point[0] + 1
                ))
        elif self.language == 'swift':
            text = self._get_text(node)
            if text.startswith('import '):
                module = text[7:].strip()
                imports.append(self.ImportDef(
                    module=module,
                    lineno=node.start_point[0] + 1
                ))
        return imports

    def _parse_attribute(self, node) -> List[Any]:
        attrs = []
        visibility = "public"
        type_annot = None

        if self.language == 'java':
            mods = self._find_child_by_type(node, 'modifiers')
            if mods:
                mod_text = self._get_text(mods)
                if 'private' in mod_text:
                    visibility = 'private'
                elif 'protected' in mod_text:
                    visibility = 'protected'
            type_node = node.child_by_field_name('type')
            if type_node:
                type_annot = self._get_text(type_node)
            for child in node.children:
                if child.type == 'variable_declarator':
                    name_node = child.child_by_field_name('name')
                    if name_node:
                        attrs.append(self.AttributeDef(
                            name=self._get_text(name_node),
                            type_annotation=type_annot,
                            visibility=visibility,
                            lineno=node.start_point[0] + 1,
                            col_offset=node.start_point[1]
                        ))
        elif self.language in ('javascript', 'typescript'):
            name_node = node.child_by_field_name('name')
            if name_node:
                type_node = node.child_by_field_name('type')
                if type_node:
                    type_annot = self._get_text(type_node)
                attrs.append(self.AttributeDef(
                    name=self._get_text(name_node),
                    type_annotation=type_annot,
                    lineno=node.start_point[0] + 1,
                    col_offset=node.start_point[1]
                ))
        elif self.language == 'cpp':
            type_node = node.child_by_field_name('type')
            if type_node:
                type_annot = self._get_text(type_node)
            for child in node.children:
                if child.type == 'field_declarator':
                    attrs.append(self.AttributeDef(
                        name=self._get_text(child),
                        type_annotation=type_annot,
                        lineno=node.start_point[0] + 1,
                        col_offset=node.start_point[1]
                    ))
        return attrs

    def traverse(self, node):
        node_type = node.type

        # Check Imports
        is_import = False
        if node_type in ('import_statement', 'import_from_statement', 'import_declaration',
                          'preproc_include', 'use_declaration'):
            is_import = True
            for imp in self._parse_import(node):
                self.module.imports.append(imp)

        # Check Class Definition
        is_class = False
        if node_type in ('class_definition', 'class_declaration', 'enum_declaration',
                          'class_specifier', 'struct_specifier', 'struct_item', 'enum_item',
                          'union_item', 'class_declaration', 'struct_declaration', 'enum_declaration'):
            is_class = True
            cls = self._parse_class(node)
            if self.class_stack:
                self.class_stack[-1].nested_classes.append(cls)
            else:
                self.module.classes.append(cls)
            self.class_stack.append(cls)

        elif node_type == 'type_spec' and self.language == 'go':
            is_struct = any(c.type == 'struct_type' for c in node.children)
            is_iface = any(c.type == 'interface_type' for c in node.children)
            if is_struct or is_iface:
                is_class = True
                cls = self._parse_class(node)
                if is_iface:
                    cls.is_interface = True
                    iface = self.InterfaceDef(
                        name=cls.name,
                        lineno=cls.lineno,
                        col_offset=cls.col_offset
                    )
                    self.module.interfaces.append(iface)
                    self.interface_stack.append(iface)
                else:
                    self.module.classes.append(cls)
                    self.class_stack.append(cls)

        # Check Interface Definition (non-Go)
        is_interface = False
        if node_type in ('interface_declaration', 'trait_item', 'protocol_declaration') and not is_class:
            is_interface = True
            iface = self._parse_interface(node)
            self.module.interfaces.append(iface)
            self.interface_stack.append(iface)

        # Check Rust impl item
        is_rust_impl = False
        rust_impl_class_name = None
        if node_type == 'impl_item' and self.language == 'rust':
            is_rust_impl = True
            type_node = node.child_by_field_name('type')
            if type_node:
                rust_impl_class_name = self._get_text(type_node)
                if '<' in rust_impl_class_name:
                    rust_impl_class_name = rust_impl_class_name.split('<')[0].strip()

        # Check Function / Method Definition
        is_func = False
        if node_type in ('function_definition', 'method_declaration', 'constructor_declaration',
                          'method_definition', 'function_declaration', 'generator_function_declaration',
                          'arrow_function', 'function_item'):
            is_func = True
            func = self._parse_function(node)

            if self.class_stack:
                self.class_stack[-1].methods.append(func)
            elif self.interface_stack:
                self.interface_stack[-1].methods.append(func)
            elif self.language == 'go' and node_type == 'method_declaration':
                receiver = node.child_by_field_name('receiver')
                receiver_type = ""
                if receiver:
                    # Traversal helper to find type identifier
                    for child in receiver.walk():
                        if child.type in ('type_identifier', 'identifier'):
                            receiver_type = self._get_text(child)
                            break
                if receiver_type:
                    receiver_type = receiver_type.lstrip('*')
                    self.out_of_line_methods[receiver_type].append(func)
                else:
                    self.module.functions.append(func)
            else:
                self.module.functions.append(func)

        # Check Attribute / Field
        is_attr = False
        if node_type in ('field_declaration', 'public_fields_definition', 'field_definition', 'property_signature'):
            is_attr = True
            if self.class_stack:
                for attr in self._parse_attribute(node):
                    self.class_stack[-1].attributes.append(attr)

        # rust_impl pushes to class stack to nested functions can access it
        if is_rust_impl and rust_impl_class_name:
            target_cls = None
            for cls in self.module.classes:
                if cls.name == rust_impl_class_name:
                    target_cls = cls
                    break
            if not target_cls:
                target_cls = self.ClassDef(name=rust_impl_class_name)
                self.module.classes.append(target_cls)
            self.class_stack.append(target_cls)

        # Recursive traversal (skip nested details of imports/funcs/attrs)
        if not (is_import or is_func or is_attr):
            for child in node.children:
                self.traverse(child)

        # Post-traversal cleanup
        if is_class:
            if self.class_stack:
                self.class_stack.pop()
        if is_interface:
            if self.interface_stack:
                self.interface_stack.pop()
        if is_rust_impl and rust_impl_class_name:
            if self.class_stack:
                self.class_stack.pop()

    def finalize(self):
        for class_name, methods in self.out_of_line_methods.items():
            target_cls = None
            for cls in self.module.classes:
                if cls.name == class_name:
                    target_cls = cls
                    break
            if not target_cls:
                target_cls = self.ClassDef(name=class_name)
                self.module.classes.append(target_cls)
            target_cls.methods.extend(methods)


def parse_with_tree_sitter(source: str, extension: str) -> Optional[Any]:
    """
    Parses source code into a ModuleDef AST using Tree-sitter.
    Returns None if the grammar is not found or fails to load.
    """
    language = get_language_from_extension(extension)
    if language == 'unknown':
        return None

    ts_lang_name = language

    # Try to load parser using TreeSitterGrammarManager
    from src.parsers.tsgm import TreeSitterGrammarManager
    manager = TreeSitterGrammarManager()

    try:
        lang_obj = manager.get_language(ts_lang_name)
    except Exception:
        # Dynamically install/build if not available
        try:
            manager.install(ts_lang_name)
            manager.build_all()
            lang_obj = manager.get_language(ts_lang_name)
        except Exception:
            return None

    parser = tree_sitter.Parser(lang_obj)

    source_bytes = source.encode('utf-8')
    tree = parser.parse(source_bytes)
    if not tree.root_node:
        return None

    extractor = TreeSitterExtractor(source_bytes, language)
    extractor.traverse(tree.root_node)
    extractor.finalize()

    return extractor.module
