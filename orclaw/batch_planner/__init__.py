"""Batch planner — turn the issue dependency graph into parallel layers.

The planner is **pure** in spirit (no LLM, no side effects in
:mod:`algorithm`). :mod:`planner` is the impure shell that fetches
issues, calls the algorithm, and persists the result to SQLite.

See ``docs/batch-algorithm.md`` for the design.
"""

from __future__ import annotations

from orclaw.batch_planner.algorithm import (
    compute_layers,
    filter_candidates,
    next_executable_batch,
    priority_rank,
)
from orclaw.batch_planner.planner import PlannerResult, run_planner

__all__ = [
    "PlannerResult",
    "compute_layers",
    "filter_candidates",
    "next_executable_batch",
    "priority_rank",
    "run_planner",
]
