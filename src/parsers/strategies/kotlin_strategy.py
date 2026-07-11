from typing import List, Optional

from src.parsers.ast import ASTNode, ClassDef, FunctionDef, InterfaceDef
from src.parsers.strategies.base_strategy import BaseParsingStrategy


class KotlinStrategy(BaseParsingStrategy):

    def get_language(self) -> str:
        return "kotlin"

    def extract_classes(self, tree, source_bytes: bytes, filepath: str) -> List[ASTNode]:
        nodes = []

        def traverse(node, parent_class=None):
            if node.type in ['class_declaration', 'object_declaration']:
                name_node = next((child for child in node.children if child.type == 'simple_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)

                    is_data_class = False
                    modifiers = next((child for child in node.children if child.type == 'modifiers'), None)
                    if modifiers:
                        mod_text = self._extract_text(modifiers, source_bytes)
                        if 'data' in mod_text.split():
                            is_data_class = True

                    class_node = ClassDef(
                        name=name,
                        lineno=node.start_point[0] + 1,
                        end_lineno=node.end_point[0] + 1,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )
                    # Kotlin objects are essentially singletons
                    if node.type == 'object_declaration':
                        class_node.is_static = True # Map to something similar
                    if is_data_class:
                        pass # standard AST doesn't have meta yet unless we patch it, so ignore


                    nodes.append(class_node)

                    # Traverse children (e.g., nested classes, inner classes)
                    class_body = next((child for child in node.children if child.type == 'class_body'), None)
                    if class_body:
                        for child in class_body.children:
                            traverse(child, class_node)

            elif node.type == 'interface_declaration':
                name_node = next((child for child in node.children if child.type == 'simple_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)
                    interface_node = InterfaceDef(
                        name=name,
                        lineno=node.start_point[0] + 1,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )
                    nodes.append(interface_node)

            else:
                for child in node.children:
                    traverse(child, parent_class)

        traverse(tree.root_node)
        return nodes

    def extract_functions(self, tree, source_bytes: bytes, filepath: str, parent: Optional[ASTNode] = None) -> List[ASTNode]:
        nodes = []

        def traverse(node, current_parent):
            if node.type == 'function_declaration':
                name_node = next((child for child in node.children if child.type == 'simple_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)

                    if current_parent and isinstance(current_parent, (ClassDef, InterfaceDef)):
                        func_node = FunctionDef(
                            name=name,
                            lineno=node.start_point[0] + 1,
                            end_lineno=node.end_point[0] + 1,
                            start_byte=node.start_byte,
                            end_byte=node.end_byte,
                        )
                        # Store docstring in meta or as a custom field if we want to preserve it,
                        # but standard AST doesn't have docstring field directly
                        current_parent.methods.append(func_node)
                    else:
                        func_node = FunctionDef(
                            name=name,
                            lineno=node.start_point[0] + 1,
                            end_lineno=node.end_point[0] + 1,
                            start_byte=node.start_byte,
                            end_byte=node.end_byte,
                        )

                    # Extract body text for further parsing (like called methods)
                    body = next((child for child in node.children if child.type == 'function_body'), None)
                    if body:
                        func_node.body_text = self._extract_text(body, source_bytes)

                    nodes.append(func_node)

            # Maintain parent context
            next_parent = current_parent
            if node.type in ['class_declaration', 'object_declaration', 'interface_declaration']:
                name_node = next((child for child in node.children if child.type == 'simple_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)
                    # Create a dummy parent to pass down the name
                    next_parent = ClassDef(name=name, lineno=0)

            for child in node.children:
                traverse(child, next_parent)

        traverse(tree.root_node, parent)
        return nodes
