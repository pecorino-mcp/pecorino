;; Complexity
[
  (if_statement)
  (while_statement)
  (for_statement)
  (do_statement)
  (switch_statement)
  (case_statement)
  (catch_clause)
  (binary_expression operator: "&&")
  (binary_expression operator: "||")
] @complexity

;; Types
[
  (qualified_identifier)
  (field_expression)
] @type.compound
[
  (identifier)
  (type_identifier)
] @type.leaf
