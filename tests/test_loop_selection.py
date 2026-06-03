"""Tests for the pure helpers inside ``orchestrator.loop``.

These do not exercise the HTTP / DB side — they assert that the
selection logic (which PRs need review, idle reasons, …) does the right
thing on synthetic inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from orclaw.config import ConcurrencySettings, Settings
from orclaw.models import PullRequest
from orclaw.orchestrator.loop import (
    OrchestratorDecision,
    _issues_referenced_by_prs,
    describe_next_action,
    select_prs_needing_review,
)
from orclaw.orchestrator.state import BatchSnapshot, OrchestratorState


def _pr(*, number: int, labels: tuple[str, ...] = (), closing: tuple[int, ...] = ()) -> PullRequest:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    return PullRequest(
        number=number,
        title=f"pr-{number}",
        body="",
        head_ref=f"feat/{number}",
        base_ref="develop",
        state="open",
        merged=False,
        labels=frozenset(labels),
        closing_issues=frozenset(closing),
        created_at=now,
        updated_at=now,
    )


class TestSelectPrsNeedingReview:
    def test_empty_input_yields_empty(self) -> None:
        assert select_prs_needing_review([]) == []

    def test_picks_unlabelled_prs(self) -> None:
        prs = [_pr(number=1), _pr(number=2)]
        result = select_prs_needing_review(prs)
        assert [pr.number for pr in result] == [1, 2]

    def test_skips_review_pending(self) -> None:
        prs = [
            _pr(number=1),
            _pr(number=2, labels=("review:pending",)),
            _pr(number=3),
        ]
        result = select_prs_needing_review(prs)
        assert [pr.number for pr in result] == [1, 3]

    def test_skips_review_approved(self) -> None:
        prs = [_pr(number=1, labels=("review:approved",))]
        assert select_prs_needing_review(prs) == []

    def test_skips_review_needs_changes(self) -> None:
        prs = [_pr(number=1, labels=("review:needs-changes",))]
        assert select_prs_needing_review(prs) == []

    def test_skips_review_hard_block(self) -> None:
        prs = [_pr(number=1, labels=("review:hard-block",))]
        assert select_prs_needing_review(prs) == []

    def test_skips_requires_human_review(self) -> None:
        # Opt-out label means the orchestrator should not auto-review.
        prs = [_pr(number=1, labels=("requires-human-review",))]
        assert select_prs_needing_review(prs) == []

    def test_preserves_order(self) -> None:
        # We want oldest PRs first; GH returns them in numeric order, so we
        # just preserve list order.
        prs = [_pr(number=5), _pr(number=2), _pr(number=8)]
        result = select_prs_needing_review(prs)
        assert [pr.number for pr in result] == [5, 2, 8]


def _empty_batches() -> BatchSnapshot:
    return BatchSnapshot(
        layers=[],
        status_by_issue={},
        issues_pending=frozenset(),
        issues_in_progress=frozenset(),
        issues_merged=frozenset(),
        issues_failed=frozenset(),
        issues_skipped=frozenset(),
    )


def _state(*, paused: bool = False, active: int = 0, cap: int = 1) -> OrchestratorState:
    return OrchestratorState(
        paused=paused,
        last_planner_run=None,
        batches=_empty_batches(),
        active_run_count=active,
        effective_max_in_flight=cap,
    )


def _decision(
    state: OrchestratorState,
    *,
    layer_index: int = -1,
    issues: tuple[int, ...] = (),
    prs: tuple[int, ...] = (),
) -> OrchestratorDecision:
    return OrchestratorDecision(
        layer_index=layer_index,
        issues_to_dispatch=list(issues),
        reason="test",
        state=state,
        prs_to_review=list(prs),
    )


_SETTINGS = Settings(concurrency=ConcurrencySettings(max_in_flight=1))


class TestDescribeNextAction:
    def test_paused_short_circuits(self) -> None:
        # Even with work queued, a paused engine does nothing.
        d = _decision(_state(paused=True), prs=(7,), issues=(3,))
        assert describe_next_action(d, _SETTINGS) == "Paused — no dispatch until resumed"

    def test_waiting_when_slot_full(self) -> None:
        # Single-flight: one task in flight (cap 1) → next tick waits.
        d = _decision(_state(active=1, cap=1), prs=(7,))
        assert describe_next_action(d, _SETTINGS) == "Waiting — 1 task in flight (cap 1)"

    def test_waiting_pluralises(self) -> None:
        d = _decision(_state(active=2, cap=2))
        assert describe_next_action(d, _SETTINGS) == "Waiting — 2 tasks in flight (cap 2)"

    def test_review_outranks_implement(self) -> None:
        # Reviewer pass runs first, so a PR awaiting review wins.
        d = _decision(_state(active=0, cap=1), layer_index=0, issues=(3,), prs=(7, 8))
        assert describe_next_action(d, _SETTINGS) == "Review PR #7"

    def test_implement_when_no_reviews(self) -> None:
        d = _decision(_state(active=0, cap=1), layer_index=2, issues=(3, 4))
        assert describe_next_action(d, _SETTINGS) == "Implement issue #3 (layer 2)"

    def test_idle_when_nothing_ready(self) -> None:
        d = _decision(_state(active=0, cap=1))
        assert describe_next_action(d, _SETTINGS) == "Idle — nothing ready to dispatch"


class TestIssuesReferencedByPrs:
    def test_empty(self) -> None:
        assert _issues_referenced_by_prs([]) == frozenset()

    def test_collects_closing_issues(self) -> None:
        prs = [
            _pr(number=1, closing=(88,)),
            _pr(number=2, closing=(91,)),
            _pr(number=3, closing=(92, 93)),
        ]
        assert _issues_referenced_by_prs(prs) == frozenset({88, 91, 92, 93})
