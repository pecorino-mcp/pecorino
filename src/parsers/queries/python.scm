;; Complexity
[
  (if_statement)
  (while_statement)
  (for_statement)
  (except_clause)
  (match_statement)
  (case_clause)
  (boolean_operator)
] @complexity

;; Types
[
  (attribute)
] @type.compound
[
  (identifier)
  (type)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(class_definition) @graph.class
(function_definition) @graph.function
(import_statement) @graph.import
(import_from_statement) @graph.import
(call) @graph.call
