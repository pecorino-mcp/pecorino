;; Complexity
[
  (if_statement)
  (while_statement)
  (for_statement)
  (enhanced_for_statement)
  (do_statement)
  (switch_expression)
  (switch_statement)
  (switch_label)
  (catch_clause)
  (binary_expression operator: "&&")
  (binary_expression operator: "||")
] @complexity

;; Types
[
  (scoped_identifier)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(class_declaration) @graph.class
(interface_declaration) @graph.interface
(enum_declaration) @graph.class
(method_declaration) @graph.function
(constructor_declaration) @graph.function
(import_declaration) @graph.import
(method_invocation) @graph.call
