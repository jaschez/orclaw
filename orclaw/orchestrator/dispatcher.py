"""Implementer dispatcher — turns an orchestrator decision into real GitHub actions.

Sprint 3 adds the *side-effect* layer the loop has been missing:

1. Render the implementer prompt template (``prompts/implementer-comment.md``)
2. POST the ``@claude implement`` comment on the target issue
3. Add the ``agent:start`` label (so cleanup-agent-start can guard re-pick)
4. Insert a ``runs`` row (status=queued) for observability
5. Promote the ``batches`` row to ``in_progress``

Steps 4-5 happen **after** the GitHub side has accepted the comment, in a
single transaction. If GitHub posts succeed but the DB write fails we
prefer to log loudly + keep the orchestrator paused-on-error (sprint 4
will add automatic recovery).

The dispatcher is **idempotent at the orchestrator level**: callers must
pass an issue that is *not* already `in_progress` and *not* referenced by
an open PR. Both guarantees come from :func:`next_executable_batch`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orclaw.exceptions import OrclawError
from orclaw.logging import get_logger
from orclaw.orchestrator.state import create_run, mark_batch_in_progress

if TYPE_CHECKING:
    import sqlite3

    from orclaw.github_client import GitHubClient
    from orclaw.models import Issue, PullRequest

log = get_logger(__name__)


# --- Constants -------------------------------------------------------------

#: Default model to record in the ``runs`` row. The Pro plan picks the
#: actual model server-side; this is just a label for telemetry.
DEFAULT_MODEL = "claude-sonnet-4-6"

#: Label applied to the issue at dispatch time. Mirrors the convention used
#: by the workflows in the ${TARGET_REPO} repo.
AGENT_START_LABEL = "agent:start"

#: Label applied to a PR when the reviewer is dispatched. The reviewer
#: itself replaces this with ``review:approved`` / ``review:needs-changes``
#: / ``review:hard-block`` when it finishes.
REVIEW_PENDING_LABEL = "review:pending"

#: Any of these labels means the PR has already gone through the reviewer
#: (or has been opted out manually). Dispatch is skipped for these.
REVIEW_TERMINAL_LABELS = frozenset(
    {
        "review:approved",
        "review:minor-fixes-applied",
        "review:needs-changes",
        "review:hard-block",
        "requires-human-review",  # explicit opt-out
    }
)

#: Path to the prompt templates. Resolved relative to repo root.
#: Each call routes through the overlay layer so dashboard edits to
#: ``/var/lib/orclaw/overrides/prompts/`` win over the git-tracked
#: defaults without touching the repo.
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "implementer-comment.md"
_REVIEWER_PROMPT_PATH = _PROMPTS_DIR / "reviewer-comment.md"
_IMPLEMENTER_PROMPT_NAME = "implementer-comment.md"
_REVIEWER_PROMPT_NAME = "reviewer-comment.md"


# --- Errors ----------------------------------------------------------------


class DispatchSkippedError(OrclawError):
    """Raised when the dispatcher refuses to act (already in progress, etc.).

    Callers should treat this as a benign skip — log it and move on to the
    next issue in the batch.
    """


# --- Result DTO ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What the dispatcher produced for one issue."""

    issue_number: int
    run_id: str
    comment_id: int
    rendered_prompt: str
    """The full prompt that was posted, captured for audit."""


# --- Helpers ---------------------------------------------------------------


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _short_slug(title: str, *, max_len: int = 40) -> str:
    """Kebab-case slug for branch names. Lowercase, max ``max_len`` chars.

    Empty titles yield ``"untitled"`` so callers can always build a branch
    name without a None-check.
    """
    cleaned = _SLUG_NON_ALNUM.sub("-", title.lower()).strip("-")
    if not cleaned:
        return "untitled"
    return cleaned[:max_len].rstrip("-") or "untitled"


def _load_prompt_template(explicit_path: Path | None, default_name: str) -> str:
    """Load a prompt template, honouring the overlay layer.

    - When ``explicit_path`` is set (tests use this), read it verbatim.
    - Otherwise route through :mod:`orclaw.overlay` so dashboard
      edits to ``/var/lib/orclaw/overrides/prompts/<name>`` win
      over the git-tracked default. Falls back transparently to the
      shipped template when the user hasn't customised it.
    """
    if explicit_path is not None:
        return explicit_path.read_text(encoding="utf-8")
    from orclaw import overlay

    content, _is_override = overlay.read_prompt(overlay.default_paths(), default_name)
    return content


def render_implementer_prompt(
    issue: Issue,
    *,
    orchestrator_run_id: str,
    template_path: Path | None = None,
) -> str:
    """Render the implementer @claude comment for one issue.

    Variables substituted: ``{{ISSUE_NUMBER}}``, ``{{SHORT_SLUG}}``,
    ``{{ORCHESTRATOR_RUN_ID}}``. The template is loaded fresh each call
    so dashboard edits to
    ``/var/lib/orclaw/overrides/prompts/implementer-comment.md``
    (or git-tracked changes to the default) take effect on the next tick
    without restarting the orchestrator.
    """
    template = _load_prompt_template(template_path, _IMPLEMENTER_PROMPT_NAME)
    return (
        template.replace("{{ISSUE_NUMBER}}", str(issue.number))
        .replace("{{SHORT_SLUG}}", _short_slug(issue.title))
        .replace("{{ORCHESTRATOR_RUN_ID}}", orchestrator_run_id)
    )


# --- Public entrypoint -----------------------------------------------------


async def dispatch_implementer(
    *,
    conn: sqlite3.Connection,
    gh: GitHubClient,
    issue: Issue,
    orchestrator_run_id: str,
    model: str = DEFAULT_MODEL,
    template_path: Path | None = None,
) -> DispatchResult:
    """Dispatch one implementer run for the given issue.

    Order of operations matters: GitHub side first (so a transient DB
    failure can be reconciled by rerunning the planner), then DB writes
    within a single transaction.

    Raises :class:`DispatchSkippedError` if the issue already carries the
    ``agent:start`` label — that's a sign the orchestrator picked the same
    issue twice, which should never happen but we defend anyway.
    """
    if AGENT_START_LABEL in issue.labels:
        raise DispatchSkippedError(
            f"#{issue.number} already has '{AGENT_START_LABEL}' — skipping dispatch"
        )

    run_id = f"impl-{uuid.uuid4().hex[:12]}"
    body = render_implementer_prompt(
        issue,
        orchestrator_run_id=orchestrator_run_id,
        template_path=template_path,
    )

    log.info(
        "implementer_dispatch_started",
        issue=issue.number,
        run_id=run_id,
        orchestrator_run_id=orchestrator_run_id,
    )

    # 1. Post the @claude comment (this is what actually triggers claude.yml
    #    in the ${TARGET_REPO} repo).
    comment_id = await gh.post_comment(issue.number, body)

    # 2. Label the issue so cleanup-agent-start can guard re-pick.
    await gh.add_label(issue.number, AGENT_START_LABEL)

    # 3. Persist the run + promote the batch within one transaction so the
    #    two stay consistent (either both happen or neither).
    conn.execute("BEGIN")
    try:
        create_run(
            conn,
            run_id=run_id,
            agent="implementer",
            model=model,
            issue_number=issue.number,
            status="queued",
            notes=f"comment_id={comment_id}; orchestrator_run={orchestrator_run_id}",
        )
        mark_batch_in_progress(
            conn,
            issue_number=issue.number,
            implementer_run_id=run_id,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        log.error(
            "implementer_dispatch_db_failed",
            issue=issue.number,
            run_id=run_id,
            comment_id=comment_id,
            note="@claude comment + label already posted; DB rollback only",
        )
        raise

    log.info(
        "implementer_dispatch_completed",
        issue=issue.number,
        run_id=run_id,
        comment_id=comment_id,
    )

    return DispatchResult(
        issue_number=issue.number,
        run_id=run_id,
        comment_id=comment_id,
        rendered_prompt=body,
    )


# =========================================================================
# Reviewer dispatch
# =========================================================================


@dataclass(frozen=True, slots=True)
class ReviewerDispatchResult:
    """What the reviewer dispatcher produced for one PR."""

    pr_number: int
    issue_number: int | None
    """The issue the PR closes (via GraphQL closingIssuesReferences). May be
    None if the PR has no Closes #N link — those PRs trigger a needs-changes
    from the reviewer."""
    run_id: str
    comment_id: int
    rendered_prompt: str


def render_reviewer_prompt(
    pr: PullRequest,
    *,
    orchestrator_run_id: str,
    template_path: Path | None = None,
) -> str:
    """Render the reviewer @claude comment for one PR.

    Variables: ``{{PR_NUMBER}}``, ``{{ISSUE_NUMBER}}`` (resolved from the
    PR's closingIssuesReferences; falls back to ``?`` when none),
    ``{{ORCHESTRATOR_RUN_ID}}``.
    """
    template = _load_prompt_template(template_path, _REVIEWER_PROMPT_NAME)
    # We render the FIRST closing issue (PRs in our pipeline should close
    # exactly one issue; if a human opened a multi-closer PR the reviewer
    # will flag it anyway).
    issue_number = next(iter(sorted(pr.closing_issues)), None)
    issue_str = str(issue_number) if issue_number is not None else "?"
    return (
        template.replace("{{PR_NUMBER}}", str(pr.number))
        .replace("{{ISSUE_NUMBER}}", issue_str)
        .replace("{{ORCHESTRATOR_RUN_ID}}", orchestrator_run_id)
    )


async def dispatch_reviewer(
    *,
    conn: sqlite3.Connection,
    gh: GitHubClient,
    pr: PullRequest,
    orchestrator_run_id: str,
    model: str = DEFAULT_MODEL,
    template_path: Path | None = None,
) -> ReviewerDispatchResult:
    """Dispatch the reviewer for one PR.

    Skip semantics mirror the implementer dispatcher: if the PR already
    carries a ``review:*`` terminal label (or ``requires-human-review``),
    raise :class:`DispatchSkippedError`. The orchestrator filters those
    out upstream too, but we defend in depth.
    """
    overlap = pr.labels & REVIEW_TERMINAL_LABELS
    if overlap:
        raise DispatchSkippedError(
            f"PR #{pr.number} already carries {sorted(overlap)} — skipping reviewer"
        )
    if REVIEW_PENDING_LABEL in pr.labels:
        raise DispatchSkippedError(
            f"PR #{pr.number} already has '{REVIEW_PENDING_LABEL}' — reviewer in flight"
        )

    run_id = f"rev-{uuid.uuid4().hex[:12]}"
    body = render_reviewer_prompt(
        pr,
        orchestrator_run_id=orchestrator_run_id,
        template_path=template_path,
    )
    issue_number = next(iter(sorted(pr.closing_issues)), None)

    log.info(
        "reviewer_dispatch_started",
        pr=pr.number,
        issue=issue_number,
        run_id=run_id,
        orchestrator_run_id=orchestrator_run_id,
    )

    # 1. Post the @claude review comment on the PR (issues + PRs share the
    #    /issues/{n}/comments endpoint, so post_comment works for both).
    comment_id = await gh.post_comment(pr.number, body)

    # 2. Label the PR so we don't re-dispatch.
    await gh.add_label(pr.number, REVIEW_PENDING_LABEL)

    # 3. Persist the run + a pending reviews row in one transaction.
    conn.execute("BEGIN")
    try:
        create_run(
            conn,
            run_id=run_id,
            agent="reviewer",
            model=model,
            issue_number=issue_number,
            pr_number=pr.number,
            status="queued",
            notes=f"comment_id={comment_id}; orchestrator_run={orchestrator_run_id}",
        )
        # NOTE: we don't insert into `reviews` here. That table tracks the
        # *verdict* (approved / needs-changes / …), which we won't know
        # until the reviewer agent comes back. A future sprint adds a
        # poll-back step that reads the PR's labels + final comment and
        # then INSERTs the reviews row.
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        log.error(
            "reviewer_dispatch_db_failed",
            pr=pr.number,
            run_id=run_id,
            comment_id=comment_id,
            note="@claude comment + label already posted; DB rollback only",
        )
        raise

    log.info(
        "reviewer_dispatch_completed",
        pr=pr.number,
        run_id=run_id,
        comment_id=comment_id,
    )

    return ReviewerDispatchResult(
        pr_number=pr.number,
        issue_number=issue_number,
        run_id=run_id,
        comment_id=comment_id,
        rendered_prompt=body,
    )
