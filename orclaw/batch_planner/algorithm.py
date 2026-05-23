"""Pure-function batch planning.

No I/O, no logging, no globals. Everything is a deterministic function of
its inputs so the algorithm is trivially testable with synthetic data.

The output is a list of "layers": ``layers[k]`` is the list of issue
numbers safe to implement in parallel once all earlier layers have closed.

See ``docs/batch-algorithm.md`` for the design rationale + worked example.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from orclaw.dependency_parser import parse_blocked_by
from orclaw.exceptions import DependencyCycleError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from orclaw.models import Issue

# Labels that **always** exclude an issue from the orchestrated pipeline.
# `ops` issues are human-only; `do-not-implement` is a per-issue kill
# switch; `wontfix` is self-explanatory.
EXCLUDED_LABELS: frozenset[str] = frozenset({"ops", "do-not-implement", "wontfix"})


def priority_rank(issue: Issue) -> int:
    """Lower rank = picked first within a layer.

    P0 → 0, P1 → 1, everything else → 2. Tie-breaks by issue number
    (oldest first) happen at the caller level.
    """
    labels = issue.labels
    if "P0" in labels:
        return 0
    if "P1" in labels:
        return 1
    return 2


def filter_candidates(issues: Iterable[Issue]) -> dict[int, Issue]:
    """Restrict the input set to issues the orchestrator should consider.

    Excludes:

    - Closed issues
    - Issues carrying any label in :data:`EXCLUDED_LABELS`

    Returns a mapping ``{issue_number: Issue}`` (preserves no ordering
    deliberately — the caller sorts).
    """
    return {
        issue.number: issue
        for issue in issues
        if issue.is_open and not (issue.labels & EXCLUDED_LABELS)
    }


def compute_layers(issues: Iterable[Issue]) -> list[list[int]]:
    """Topological layering of the dependency graph (Kahn's algorithm).

    Layer 0 = issues whose declared blockers are all already closed
    (or never existed). Layer K = issues whose blockers are all in
    layers strictly less than K.

    Within a layer, issues are sorted by ``(priority_rank, number)`` so
    P0 work surfaces before P1 and ties are deterministic.

    Raises
    ------
    DependencyCycleError
        If at any point the remaining set is non-empty but no issue has
        zero in-degree — that means the dependency graph has a cycle.
    """
    candidates = filter_candidates(issues)
    if not candidates:
        return []

    candidate_numbers = frozenset(candidates)

    # For each candidate, the set of blockers that are still OPEN within
    # the candidate set. Blockers outside the candidate set are treated as
    # "already resolved" — they're either closed, excluded, or unknown.
    deps: dict[int, frozenset[int]] = {}
    for number, issue in candidates.items():
        declared = parse_blocked_by(issue.body)
        deps[number] = declared & candidate_numbers

    # Reverse edges: blocker → set of issues blocked by it.
    blocks: dict[int, set[int]] = defaultdict(set)
    for number, blockers in deps.items():
        for blocker in blockers:
            blocks[blocker].add(number)

    in_degree = {number: len(deps[number]) for number in candidates}
    remaining: set[int] = set(candidates)
    layers: list[list[int]] = []

    while remaining:
        layer = sorted(
            (n for n in remaining if in_degree[n] == 0),
            key=lambda n: (priority_rank(candidates[n]), n),
        )
        if not layer:
            raise DependencyCycleError(sorted(remaining))

        layers.append(layer)
        for number in layer:
            remaining.discard(number)
            for unblocked in blocks[number]:
                in_degree[unblocked] -= 1

    return layers


def next_executable_batch(
    layers: list[list[int]],
    *,
    issues_with_open_pr: frozenset[int],
    issues_in_progress: frozenset[int],
    concurrency_cap: int,
) -> tuple[int, list[int]]:
    """Pick the first layer that has work left to do, capped by concurrency.

    Returns
    -------
    (layer_index, issue_numbers)
        - ``layer_index`` is the index of the layer chosen (0-based)
        - ``issue_numbers`` is the slice of that layer the orchestrator
          should kick off now (max ``concurrency_cap`` items)

    If every layer is fully in-progress or merged, returns ``(-1, [])``
    — the orchestrator should idle.
    """
    if concurrency_cap < 1:
        raise ValueError(f"concurrency_cap must be >= 1, got {concurrency_cap}")

    blocked = issues_with_open_pr | issues_in_progress

    for index, layer in enumerate(layers):
        available = [n for n in layer if n not in blocked]
        if not available:
            # This layer is either fully done or already in flight elsewhere.
            # Advance to the next layer only if EVERY issue in this layer
            # is in `blocked` (otherwise the missing ones are "merged" /
            # outside the candidate set and we don't need to wait).
            if all(n in blocked for n in layer):
                continue
            # Layer has items but none available — wait.
            return (-1, [])
        return (index, available[:concurrency_cap])

    return (-1, [])
