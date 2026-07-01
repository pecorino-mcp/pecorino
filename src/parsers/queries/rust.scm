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
