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
