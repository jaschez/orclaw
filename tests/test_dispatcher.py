"""Tests for the implementer + reviewer dispatchers.

These tests use a fake :class:`GitHubClient` so no real HTTP traffic
happens. The dispatchers' job is to (1) post the right comment, (2)
label the issue/PR, and (3) keep the DB and GitHub side in sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from orclaw.db import connect, init_db
from orclaw.models import BatchStatus, PullRequest, RunStatus
from orclaw.orchestrator.dispatcher import (
    AGENT_START_LABEL,
    REVIEW_PENDING_LABEL,
    DispatchSkippedError,
    _short_slug,
    dispatch_implementer,
    dispatch_reviewer,
    render_implementer_prompt,
    render_reviewer_prompt,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Fake GitHub client ----------------------------------------------------


class FakeGitHubClient:
    """Records calls instead of hitting GitHub.

    We only fake the surface the dispatcher exercises: ``post_comment``
    and ``add_label``.
    """

    def __init__(self, *, next_comment_id: int = 1001, repo: str = "owner/repo") -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, tuple[str, ...]]] = []
        self._next_comment_id = next_comment_id
        self.repo = repo

    async def post_comment(self, issue_or_pr_number: int, body: str) -> int:
        cid = self._next_comment_id
        self._next_comment_id += 1
        self.comments.append((issue_or_pr_number, body))
        return cid

    async def add_label(self, issue_or_pr_number: int, *labels: str) -> None:
        self.labels.append((issue_or_pr_number, labels))


# --- _short_slug -----------------------------------------------------------


class TestShortSlug:
    def test_lowercases_and_kebabs(self) -> None:
        assert _short_slug("Add Stripe Connect") == "add-stripe-connect"

    def test_strips_punctuation(self) -> None:
        assert _short_slug("Fix: weird/edge-case!!!") == "fix-weird-edge-case"

    def test_truncates_long_titles(self) -> None:
        slug = _short_slug("a" * 100, max_len=40)
        assert len(slug) == 40

    def test_empty_input_yields_untitled(self) -> None:
        assert _short_slug("") == "untitled"
        assert _short_slug("---") == "untitled"


# --- render_implementer_prompt --------------------------------------------


class TestRenderPrompt:
    def test_substitutes_all_variables(self, issue_factory) -> None:
        issue = issue_factory(number=88, title="Add checkout flow")
        body = render_implementer_prompt(issue, orchestrator_run_id="abc123")
        assert "@claude implement" in body
        assert "#88" in body
        assert "feat/88-add-checkout-flow" in body
        assert "abc123" in body
        # No leftover template markers.
        assert "{{" not in body
        assert "}}" not in body

    def test_uses_custom_template(self, issue_factory, tmp_path: Path) -> None:
        custom = tmp_path / "tpl.md"
        custom.write_text(
            "issue={{ISSUE_NUMBER}} slug={{SHORT_SLUG}} run={{ORCHESTRATOR_RUN_ID}}",
            encoding="utf-8",
        )
        issue = issue_factory(number=42, title="Some title")
        body = render_implementer_prompt(issue, orchestrator_run_id="xyz", template_path=custom)
        assert body == "issue=42 slug=some-title run=xyz"


# --- dispatch_implementer --------------------------------------------------


@pytest.fixture()
def seeded_db(tmp_data_dir: Path) -> Path:
    """Init schema + insert one pending batch row for issue #88."""
    db_path = tmp_data_dir / "engine.db"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
            (0, 88, BatchStatus.PENDING.value),
        )
    return db_path


class TestDispatchImplementer:
    @pytest.mark.asyncio
    async def test_happy_path_posts_comment_and_promotes_batch(
        self,
        seeded_db: Path,
        issue_factory,
        tmp_path: Path,
    ) -> None:
        # Use a minimal custom template so we don't depend on the real
        # prompts/ file content for this test.
        tpl = tmp_path / "tpl.md"
        tpl.write_text("@claude implement #{{ISSUE_NUMBER}}", encoding="utf-8")

        gh = FakeGitHubClient()
        issue = issue_factory(number=88, title="Schema baseline")

        with connect(seeded_db) as conn:
            result = await dispatch_implementer(
                conn=conn,
                gh=gh,  # type: ignore[arg-type]
                issue=issue,
                orchestrator_run_id="orch-1",
                template_path=tpl,
            )

        # GitHub side
        assert len(gh.comments) == 1
        assert gh.comments[0][0] == 88
        assert "@claude implement #88" in gh.comments[0][1]
        assert gh.labels == [(88, (AGENT_START_LABEL,))]
        assert result.comment_id == 1001
        assert result.run_id.startswith("impl-")

        # DB side
        with connect(seeded_db, read_only=True) as conn:
            row = conn.execute(
                "SELECT status, implementer_run_id FROM batches WHERE issue_number = 88"
            ).fetchone()
            assert row["status"] == BatchStatus.IN_PROGRESS.value
            assert row["implementer_run_id"] == result.run_id

            run_row = conn.execute(
                "SELECT agent, status, issue_number, notes, repo FROM runs WHERE id = ?",
                (result.run_id,),
            ).fetchone()
            assert run_row["agent"] == "implementer"
            assert run_row["status"] == RunStatus.QUEUED.value
            assert run_row["issue_number"] == 88
            assert "comment_id=1001" in run_row["notes"]
            # Phase 1: the run is tagged with the target repo.
            assert run_row["repo"] == "owner/repo"

    @pytest.mark.asyncio
    async def test_refuses_if_agent_start_already_set(
        self,
        seeded_db: Path,
        issue_factory,
        tmp_path: Path,
    ) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text("noop", encoding="utf-8")

        gh = FakeGitHubClient()
        issue = issue_factory(number=88, title="Already in flight", labels=(AGENT_START_LABEL,))

        with connect(seeded_db) as conn, pytest.raises(DispatchSkippedError):
            await dispatch_implementer(
                conn=conn,
                gh=gh,  # type: ignore[arg-type]
                issue=issue,
                orchestrator_run_id="orch-1",
                template_path=tpl,
            )

        # Nothing should have been posted.
        assert gh.comments == []
        assert gh.labels == []

        # Batch should still be pending.
        with connect(seeded_db, read_only=True) as conn:
            row = conn.execute("SELECT status FROM batches WHERE issue_number = 88").fetchone()
            assert row["status"] == BatchStatus.PENDING.value


# =========================================================================
# Reviewer dispatcher
# =========================================================================


def _pr(
    *,
    number: int = 200,
    closing: tuple[int, ...] = (88,),
    labels: tuple[str, ...] = (),
    title: str = "feat(schema): add events table",
) -> PullRequest:
    """Build a PullRequest for tests."""
    now = datetime(2026, 5, 22, tzinfo=UTC)
    return PullRequest(
        number=number,
        title=title,
        body=f"Closes #{closing[0]}" if closing else "",
        head_ref=f"feat/{closing[0] if closing else 'misc'}-x",
        base_ref="develop",
        state="open",
        merged=False,
        labels=frozenset(labels),
        closing_issues=frozenset(closing),
        created_at=now,
        updated_at=now,
    )


class TestRenderReviewerPrompt:
    def test_substitutes_pr_and_issue(self, tmp_path: Path) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text(
            "pr={{PR_NUMBER}} issue={{ISSUE_NUMBER}} run={{ORCHESTRATOR_RUN_ID}}",
            encoding="utf-8",
        )
        pr = _pr(number=200, closing=(88,))
        body = render_reviewer_prompt(pr, orchestrator_run_id="abc", template_path=tpl)
        assert body == "pr=200 issue=88 run=abc"

    def test_falls_back_to_question_mark_when_no_closing_issue(self, tmp_path: Path) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text("issue={{ISSUE_NUMBER}}", encoding="utf-8")
        pr = _pr(closing=())
        body = render_reviewer_prompt(pr, orchestrator_run_id="abc", template_path=tpl)
        assert body == "issue=?"

    def test_picks_lowest_issue_number_when_multiple_closers(self, tmp_path: Path) -> None:
        # Multi-closer PRs shouldn't happen in our pipeline but we handle it.
        tpl = tmp_path / "tpl.md"
        tpl.write_text("issue={{ISSUE_NUMBER}}", encoding="utf-8")
        pr = _pr(closing=(50, 30, 99))
        body = render_reviewer_prompt(pr, orchestrator_run_id="abc", template_path=tpl)
        assert body == "issue=30"


class TestDispatchReviewer:
    @pytest.mark.asyncio
    async def test_happy_path_posts_review_and_records_run(
        self, seeded_db: Path, tmp_path: Path
    ) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text("@claude review PR #{{PR_NUMBER}}", encoding="utf-8")

        gh = FakeGitHubClient(next_comment_id=2001)
        pr = _pr(number=200, closing=(88,))

        with connect(seeded_db) as conn:
            result = await dispatch_reviewer(
                conn=conn,
                gh=gh,  # type: ignore[arg-type]
                pr=pr,
                orchestrator_run_id="orch-rev-1",
                template_path=tpl,
            )

        # GitHub side
        assert gh.comments == [(200, "@claude review PR #200")]
        assert gh.labels == [(200, (REVIEW_PENDING_LABEL,))]
        assert result.run_id.startswith("rev-")
        assert result.issue_number == 88
        assert result.comment_id == 2001

        # DB side: runs row only (reviews row lands later when verdict
        # is parsed back).
        with connect(seeded_db, read_only=True) as conn:
            run_row = conn.execute(
                "SELECT agent, status, pr_number, issue_number FROM runs WHERE id = ?",
                (result.run_id,),
            ).fetchone()
            assert run_row["agent"] == "reviewer"
            assert run_row["status"] == RunStatus.QUEUED.value
            assert run_row["pr_number"] == 200
            assert run_row["issue_number"] == 88

            reviews_count = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
            assert reviews_count["n"] == 0  # verdict not yet known

    @pytest.mark.asyncio
    async def test_refuses_if_review_pending_label_set(
        self, seeded_db: Path, tmp_path: Path
    ) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text("noop", encoding="utf-8")

        gh = FakeGitHubClient()
        pr = _pr(number=200, labels=(REVIEW_PENDING_LABEL,))

        with connect(seeded_db) as conn, pytest.raises(DispatchSkippedError):
            await dispatch_reviewer(
                conn=conn,
                gh=gh,  # type: ignore[arg-type]
                pr=pr,
                orchestrator_run_id="orch-x",
                template_path=tpl,
            )

        assert gh.comments == []
        assert gh.labels == []

    @pytest.mark.asyncio
    async def test_refuses_if_review_already_terminal(
        self, seeded_db: Path, tmp_path: Path
    ) -> None:
        tpl = tmp_path / "tpl.md"
        tpl.write_text("noop", encoding="utf-8")

        gh = FakeGitHubClient()
        pr = _pr(number=200, labels=("review:approved",))

        with connect(seeded_db) as conn, pytest.raises(DispatchSkippedError):
            await dispatch_reviewer(
                conn=conn,
                gh=gh,  # type: ignore[arg-type]
                pr=pr,
                orchestrator_run_id="orch-x",
                template_path=tpl,
            )

        assert gh.comments == []
        assert gh.labels == []
