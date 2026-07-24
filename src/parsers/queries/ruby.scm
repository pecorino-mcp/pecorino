;; Complexity
[
  (if)
  (unless)
  (while)
  (until)
  (case)
  (when)
  (rescue_modifier)
  (binary operator: "and")
  (binary operator: "or")
  (binary operator: "&&")
  (binary operator: "||")
] @complexity

;; Types
[
  (scope_resolution)
  (call)
] @type.compound
[
  (identifier)
  (constant)
] @type.leaf

;; Graph extraction — classes, functions, imports, calls
(class) @graph.class
(module) @graph.class
(method) @graph.function
(singleton_method) @graph.function
(call) @graph.call
