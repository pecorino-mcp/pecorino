;; Complexity
[
  (if_statement)
  (for_statement)
  (expression_switch_statement)
  (type_switch_statement)
  (expression_case)
  (type_case)
  (select_statement)
  (communication_case)
  (binary_expression operator: "&&")
  (binary_expression operator: "||")
] @complexity

;; Types
[
  (selector_expression)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(type_spec type: (struct_type)) @graph.class
(type_spec type: (interface_type)) @graph.interface
(function_declaration) @graph.function
(method_declaration) @graph.function
(import_declaration) @graph.import
(call_expression) @graph.call
