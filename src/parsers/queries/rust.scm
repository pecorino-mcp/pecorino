;; Complexity
[
  (if_expression)
  (while_expression)
  (for_expression)
  (loop_expression)
  (match_expression)
  (match_arm)
  (binary_expression operator: "&&")
  (binary_expression operator: "||")
] @complexity

;; Types
[
  (scoped_identifier)
  (scoped_type_identifier)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(struct_item) @graph.class
(enum_item) @graph.class
(trait_item) @graph.interface
(function_item) @graph.function
(impl_item) @graph.impl
(use_declaration) @graph.import
(call_expression) @graph.call
