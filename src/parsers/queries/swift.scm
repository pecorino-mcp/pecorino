;; Complexity
[
  (if_statement)
  (while_statement)
  (repeat_while_statement)
  (for_statement)
  (switch_statement)
  (switch_entry)
  (guard_statement)
  (catch_clause)
  (binary_expression)
] @complexity

;; Types
[
  (member_expression)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(class_declaration) @graph.class
(struct_declaration) @graph.class
(enum_declaration) @graph.class
(protocol_declaration) @graph.interface
(function_declaration) @graph.function
(import_declaration) @graph.import
(call_expression) @graph.call
