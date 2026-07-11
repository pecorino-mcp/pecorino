from typing import List, Optional

from src.parsers.ast import ASTNode, ClassDef, FunctionDef, InterfaceDef
from src.parsers.strategies.base_strategy import BaseParsingStrategy


class RustStrategy(BaseParsingStrategy):

    def get_language(self) -> str:
        return "rust"

    def extract_classes(self, tree, source_bytes: bytes, filepath: str) -> List[ASTNode]:
        nodes = []

        def traverse(node, parent_class=None):
            if node.type in ['struct_item', 'enum_item', 'impl_item']:
                # For impl blocks, the name might be a type_identifier
                name_node = next((child for child in node.children if child.type == 'type_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)

                    class_node = ClassDef(
                        name=name,
                        lineno=node.start_point[0] + 1,
                        end_lineno=node.end_point[0] + 1,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )

                    if node.type == 'enum_item':
                        pass
                    elif node.type == 'impl_item':
                        pass

                        # Check if this is a trait implementation (impl Trait for Type)
                        trait_node = next((child for child in node.children if child.type == 'type_identifier' and child != name_node), None)
                        if trait_node:
                            # It's an impl <Trait> for <Type>, so the actual target is the second one usually,
                            # but tree-sitter Rust has specific structures. Let's just flag it.
                            pass

                    nodes.append(class_node)

                    for child in node.children:
                        traverse(child, class_node)

            elif node.type == 'trait_item':
                name_node = next((child for child in node.children if child.type == 'type_identifier'), None)
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
            if node.type in ['function_item', 'function_signature_item']:
                name_node = next((child for child in node.children if child.type == 'identifier'), None)
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
                        current_parent.methods.append(func_node)
                    else:
                        func_node = FunctionDef(
                            name=name,
                            lineno=node.start_point[0] + 1,
                            end_lineno=node.end_point[0] + 1,
                            start_byte=node.start_byte,
                            end_byte=node.end_byte,
                        )

                    body = next((child for child in node.children if child.type == 'block'), None)
                    if body:
                        func_node.body_text = self._extract_text(body, source_bytes)

                    nodes.append(func_node)

            # Maintain parent context
            next_parent = current_parent
            if node.type in ['impl_item', 'trait_item']:
                name_node = next((child for child in node.children if child.type == 'type_identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)
                    next_parent = ClassDef(name=name, lineno=0)

            for child in node.children:
                traverse(child, next_parent)

        traverse(tree.root_node, parent)
        return nodes
