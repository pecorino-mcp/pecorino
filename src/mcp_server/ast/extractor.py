import os
from typing import Dict, List, Tuple, Any

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
    
    def ensure_node(self, id: str, kind: str, name: str, qualified_name: str, file: str, line: int, end_line: int, docstring: str = ""):
        if id not in self.nodes:
            self.nodes[id] = {
                "id": id,
                "kind": kind,
                "name": name,
                "qualified_name": qualified_name,
                "file": file,
                "line": line,
                "end_line": end_line,
                "docstring": docstring
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
            self._walk(tree.root_node, source, parent_id=file_node_id, parent_qname="")
            
        return self.nodes, self.edges
        
    def _walk(self, node, source: bytes, parent_id: str, parent_qname: str):
        current_id = parent_id
        current_qname = parent_qname
        
        if node.type == 'class_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = self.ts_text(name_node, source)
                current_qname = make_qname(parent_qname, name)
                current_id = make_id(self.file_path, "Class", current_qname, node.start_point[0] + 1)
                docstring = self.extract_docstring(node, source)
                self.ensure_node(current_id, "Class", name, current_qname, self.file_path, node.start_point[0] + 1, node.end_point[0] + 1, docstring)
                self.symbol_table[name] = current_id
                self.edges["CONTAINS"].append((parent_id, current_id, {}))
                
                # INHERITS
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
                                
        elif node.type == 'function_definition':
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
                
                # RETURNS
                return_type_node = node.child_by_field_name('return_type')
                if return_type_node:
                    ret_type = self.ts_text(return_type_node, source)
                    type_id = self.symbol_table.get(ret_type)
                    if not type_id:
                        ext_id = f"external_type::{ret_type}"
                        self.ensure_node(ext_id, "ExternalType", ret_type, ret_type, "__external__", 0, 0)
                        type_id = ext_id
                    self.edges["RETURNS"].append((current_id, type_id, {}))
                    
                # PARAMETER_OF
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
                                
        elif node.type == 'call_expression':
            func_node = node.child_by_field_name('function')
            if func_node:
                raw_call = self.ts_text(func_node, source)
                # Resolve simple name (last part of dotted path)
                simple_name = raw_call.split(".")[-1]
                target_id = self.symbol_table.get(simple_name)
                if not target_id:
                    ext_id = f"external::{raw_call}"
                    self.ensure_node(ext_id, "External", raw_call, raw_call, "__external__", 0, 0)
                    target_id = ext_id
                
                # CALLS uses `line` property in the edge
                self.edges["CALLS"].append((parent_id, target_id, {"line": node.start_point[0] + 1}))
                
        elif node.type in ('import_statement', 'import_from_statement'):
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
                self._walk(child, source, current_id, current_qname)
