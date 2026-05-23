"""Tests for the batch planner algorithm (pure functions, fast)."""

from __future__ import annotations

import pytest

from orclaw.batch_planner.algorithm import (
    EXCLUDED_LABELS,
    compute_layers,
    filter_candidates,
    next_executable_batch,
    priority_rank,
)
from orclaw.exceptions import DependencyCycleError

# --- priority_rank ---------------------------------------------------------


class TestPriorityRank:
    def test_p0_is_zero(self, issue_factory) -> None:
        assert priority_rank(issue_factory(labels=("P0",))) == 0

    def test_p1_is_one(self, issue_factory) -> None:
        assert priority_rank(issue_factory(labels=("P1",))) == 1

    def test_unlabelled_falls_to_two(self, issue_factory) -> None:
        assert priority_rank(issue_factory(labels=())) == 2

    def test_p0_wins_over_p1_when_both_present(self, issue_factory) -> None:
        assert priority_rank(issue_factory(labels=("P0", "P1"))) == 0


# --- filter_candidates ----------------------------------------------------


class TestFilterCandidates:
    def test_drops_closed(self, issue_factory) -> None:
        open_i = issue_factory(number=1)
        closed_i = issue_factory(number=2, state="closed")
        result = filter_candidates([open_i, closed_i])
        assert set(result.keys()) == {1}

    @pytest.mark.parametrize("excluded_label", sorted(EXCLUDED_LABELS))
    def test_drops_excluded_labels(self, issue_factory, excluded_label: str) -> None:
        keep = issue_factory(number=1)
        drop = issue_factory(number=2, labels=(excluded_label,))
        result = filter_candidates([keep, drop])
        assert set(result.keys()) == {1}

    def test_keeps_p0_with_no_excluded_label(self, issue_factory) -> None:
        i = issue_factory(number=1, labels=("P0", "area:schema"))
        result = filter_candidates([i])
        assert set(result.keys()) == {1}


# --- compute_layers --------------------------------------------------------


class TestComputeLayers:
    def test_empty_input(self) -> None:
        assert compute_layers([]) == []

    def test_single_issue_no_deps(self, issue_factory) -> None:
        i = issue_factory(number=42, labels=("P0",))
        assert compute_layers([i]) == [[42]]

    def test_two_independent_issues(self, issue_factory) -> None:
        a = issue_factory(number=1, labels=("P0",))
        b = issue_factory(number=2, labels=("P0",))
        result = compute_layers([a, b])
        assert result == [[1, 2]]

    def test_simple_chain(self, issue_factory) -> None:
        # 1 → 2 → 3 (2 blocked by 1, 3 blocked by 2)
        a = issue_factory(number=1, labels=("P0",))
        b = issue_factory(number=2, labels=("P0",), body="Blocked by #1")
        c = issue_factory(number=3, labels=("P0",), body="Blocked by #2")
        result = compute_layers([a, b, c])
        assert result == [[1], [2], [3]]

    def test_diamond(self, issue_factory) -> None:
        # 1
        # ├─ 2
        # ├─ 3
        # └─ 4 blocked by 2 AND 3
        a = issue_factory(number=1, labels=("P0",))
        b = issue_factory(number=2, labels=("P0",), body="Blocked by #1")
        c = issue_factory(number=3, labels=("P0",), body="Blocked by #1")
        d = issue_factory(number=4, labels=("P0",), body="Blocked by #2\nBlocked by #3")
        result = compute_layers([a, b, c, d])
        assert result == [[1], [2, 3], [4]]

    def test_priority_ordering_within_layer(self, issue_factory) -> None:
        # All independent; P0 should come before P1, and same-priority by id
        p1_low = issue_factory(number=1, labels=("P1",))
        p0_high = issue_factory(number=99, labels=("P0",))
        p0_low = issue_factory(number=2, labels=("P0",))
        result = compute_layers([p1_low, p0_high, p0_low])
        # Layer 0: P0 first (sorted by number), then P1
        assert result == [[2, 99, 1]]

    def test_excludes_ops_from_layers(self, issue_factory) -> None:
        # 2 depends on 1, but 1 is OPS (excluded). With our semantics, that
        # blocker becomes "outside the candidate set" → treated as resolved.
        ops = issue_factory(number=1, labels=("ops",))
        normal = issue_factory(number=2, labels=("P0",), body="Blocked by #1")
        result = compute_layers([ops, normal])
        # Only #2 in layer 0; #1 is filtered out before layering.
        assert result == [[2]]

    def test_cycle_raises(self, issue_factory) -> None:
        a = issue_factory(number=1, labels=("P0",), body="Blocked by #2")
        b = issue_factory(number=2, labels=("P0",), body="Blocked by #1")
        with pytest.raises(DependencyCycleError) as exc:
            compute_layers([a, b])
        assert set(exc.value.issues_in_cycle) == {1, 2}

    def test_dep_pointing_to_closed_issue(self, issue_factory) -> None:
        # If #1 is closed, #2 depending on it is treated as ready (deps
        # outside candidates are resolved).
        closed = issue_factory(number=1, state="closed")
        open_blocked = issue_factory(number=2, labels=("P0",), body="Blocked by #1")
        result = compute_layers([closed, open_blocked])
        assert result == [[2]]

    def test_real_world_v1_subgraph(self, issue_factory) -> None:
        # Based on docs/batch-algorithm.md worked example:
        # #88 schema, #91 emails, #98 css sanitizer
        # #92 depends on #88+#91; #97 depends on #98
        schema = issue_factory(number=88, labels=("P0",))
        emails = issue_factory(number=91, labels=("P0",))
        css = issue_factory(number=98, labels=("P0",))
        checkout = issue_factory(number=92, labels=("P0",), body="Blocked by #88\nBlocked by #91")
        branding = issue_factory(number=97, labels=("P0",), body="Blocked by #98")

        result = compute_layers([schema, emails, css, checkout, branding])
        assert result[0] == [88, 91, 98]
        # Within layer 1, sorted by number for P0 ties
        assert result[1] == [92, 97]


# --- next_executable_batch ------------------------------------------------


class TestNextExecutableBatch:
    def test_empty_layers(self) -> None:
        idx, batch = next_executable_batch(
            [],
            issues_with_open_pr=frozenset(),
            issues_in_progress=frozenset(),
            concurrency_cap=2,
        )
        assert idx == -1
        assert batch == []

    def test_picks_first_layer(self) -> None:
        idx, batch = next_executable_batch(
            [[1, 2], [3]],
            issues_with_open_pr=frozenset(),
            issues_in_progress=frozenset(),
            concurrency_cap=5,
        )
        assert idx == 0
        assert batch == [1, 2]

    def test_caps_to_concurrency(self) -> None:
        idx, batch = next_executable_batch(
            [[1, 2, 3, 4]],
            issues_with_open_pr=frozenset(),
            issues_in_progress=frozenset(),
            concurrency_cap=2,
        )
        assert idx == 0
        assert batch == [1, 2]

    def test_skips_layer_fully_in_flight(self) -> None:
        # Layer 0 = {1, 2}, both in flight → advance to layer 1
        idx, batch = next_executable_batch(
            [[1, 2], [3, 4]],
            issues_with_open_pr=frozenset({1}),
            issues_in_progress=frozenset({2}),
            concurrency_cap=2,
        )
        assert idx == 1
        assert batch == [3, 4]

    def test_waits_when_layer_partially_in_flight(self) -> None:
        # Layer 0 = {1, 2}, only #1 in flight. #2 is available but we
        # should still pick from layer 0 — return only the unblocked.
        idx, batch = next_executable_batch(
            [[1, 2], [3]],
            issues_with_open_pr=frozenset(),
            issues_in_progress=frozenset({1}),
            concurrency_cap=2,
        )
        assert idx == 0
        assert batch == [2]

    def test_idles_when_everything_blocked(self) -> None:
        idx, batch = next_executable_batch(
            [[1, 2]],
            issues_with_open_pr=frozenset({1, 2}),
            issues_in_progress=frozenset(),
            concurrency_cap=2,
        )
        assert idx == -1
        assert batch == []

    def test_rejects_zero_cap(self) -> None:
        with pytest.raises(ValueError, match="concurrency_cap"):
            next_executable_batch(
                [[1]],
                issues_with_open_pr=frozenset(),
                issues_in_progress=frozenset(),
                concurrency_cap=0,
            )
