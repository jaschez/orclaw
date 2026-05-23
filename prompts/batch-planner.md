# Batch planner — descriptor

The batch planner does NOT use Claude. It's a pure Python algorithm
(Kahn's topological sort) in `orclaw/batch_planner/`.

This file exists for symmetry with the agent prompts. The planner's
inputs / outputs / behavior are documented in
[`docs/batch-algorithm.md`](../docs/batch-algorithm.md).

If you want to customize the planner's dependency parsing (e.g. add
new keywords beyond `Depends on` / `Blocked by` / `Closes`), edit
`orclaw/batch_planner/` directly — not this file.
