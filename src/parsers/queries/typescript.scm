;; Complexity
[
  (if_statement)
  (while_statement)
  (for_statement)
  (for_in_statement)
  (do_statement)
  (switch_statement)
  (switch_case)
  (catch_clause)
  (binary_expression operator: "&&")
  (binary_expression operator: "||")
] @complexity

;; Types
[
  (member_expression)
  (nested_identifier)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(class_declaration) @graph.class
(interface_declaration) @graph.interface
(type_alias_declaration) @graph.class
(function_declaration) @graph.function
(method_definition) @graph.function
(arrow_function) @graph.function
(import_statement) @graph.import
(call_expression) @graph.call
