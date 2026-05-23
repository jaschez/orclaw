"""Tests for the pure helpers inside ``orchestrator.loop``.

These do not exercise the HTTP / DB side — they assert that the
selection logic (which PRs need review, idle reasons, …) does the right
thing on synthetic inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from orclaw.models import PullRequest
from orclaw.orchestrator.loop import (
    _issues_referenced_by_prs,
    select_prs_needing_review,
)


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
