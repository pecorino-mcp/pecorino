from typing import List, Optional
from src.parsers.ast import ASTNode, ClassDef, InterfaceDef, FunctionDef
from src.parsers.strategies.base_strategy import BaseParsingStrategy

class CSharpStrategy(BaseParsingStrategy):
    
    def get_language(self) -> str:
        return "c-sharp"
        
    def extract_classes(self, tree, source_bytes: bytes, filepath: str) -> List[ASTNode]:
        nodes = []
        
        def traverse(node, parent_class=None):
            if node.type in ['class_declaration', 'record_declaration', 'struct_declaration']:
                name_node = next((child for child in node.children if child.type == 'identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)
                    
                    class_node = ClassDef(
                        name=name,
                        lineno=node.start_point[0] + 1,
                        end_lineno=node.end_point[0] + 1,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )
                    
                    if node.type == 'record_declaration':
                        pass
                    elif node.type == 'struct_declaration':
                        pass
                        
                    nodes.append(class_node)
                    
                    for child in node.children:
                        traverse(child, class_node)
            
            elif node.type == 'interface_declaration':
                name_node = next((child for child in node.children if child.type == 'identifier'), None)
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
            if node.type in ['method_declaration', 'local_function_statement', 'constructor_declaration', 'property_declaration']:
                name_node = next((child for child in node.children if child.type == 'identifier'), None)
                
                # For constructors, tree-sitter might map the name differently
                if node.type == 'constructor_declaration':
                    name = current_parent.name if current_parent else "Constructor"
                else:
                    name = self._extract_text(name_node, source_bytes) if name_node else None
                
                if name:
                    if current_parent and isinstance(current_parent, (ClassDef, InterfaceDef)):
                        func_node = FunctionDef(
                            name=name,
                            lineno=node.start_point[0] + 1,
                            end_lineno=node.end_point[0] + 1,
                            start_byte=node.start_byte,
                            end_byte=node.end_byte,
                        )
                        if node.type == 'property_declaration':
                            pass
                        elif node.type == 'constructor_declaration':
                            pass
                            
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
            if node.type in ['class_declaration', 'interface_declaration', 'record_declaration', 'struct_declaration']:
                name_node = next((child for child in node.children if child.type == 'identifier'), None)
                if name_node:
                    name = self._extract_text(name_node, source_bytes)
                    next_parent = ClassDef(name=name, lineno=0)
                    
            for child in node.children:
                traverse(child, next_parent)

        traverse(tree.root_node, parent)
        return nodes
