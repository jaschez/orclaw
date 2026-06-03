"""Orchestrator loop — decision + (optional) dispatch.

Each tick does two passes in this order:

1. **Reviewer pass** — finds open PRs against ``develop`` that haven't
   been reviewed yet and dispatches ``@claude review`` on them. Reviewers
   are prioritised because their verdicts unblock auto-merge, which frees
   in-flight slots.
2. **Implementer pass** — picks the next executable batch from the
   layered plan and dispatches ``@claude implement`` on each issue.

Both passes share the same concurrency cap (``settings.concurrency.max_in_flight``).
Reviewer dispatches that succeed reduce the cap available to the implementer
pass in the same tick.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orclaw.batch_planner.algorithm import next_executable_batch
from orclaw.db import connect
from orclaw.github_client import GitHubAPIConfig, GitHubClient
from orclaw.logging import get_logger
from orclaw.notifications import (
    format_error_alert,
    format_saturation_alert,
    ping_healthchecks,
    post_telegram,
)
from orclaw.orchestrator.dispatcher import (
    REVIEW_PENDING_LABEL,
    REVIEW_TERMINAL_LABELS,
    DispatchResult,
    DispatchSkippedError,
    ReviewerDispatchResult,
    dispatch_implementer,
    dispatch_reviewer,
)
from orclaw.orchestrator.pollback import PollbackResult, run_pollback
from orclaw.orchestrator.state import OrchestratorState, load_state

if TYPE_CHECKING:
    import sqlite3

    from orclaw.config import Settings
    from orclaw.models import PullRequest

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrchestratorDecision:
    """The outcome of one orchestrator tick.

    Implementer side: ``issues_to_dispatch`` lists what *would* be picked
    in this tick; ``dispatched`` / ``skipped`` are populated only in apply
    mode. Reviewer side: ``prs_to_review`` lists the PRs the loop spotted;
    ``reviewers_dispatched`` / ``reviewers_skipped`` are the apply-mode
    outcomes.
    """

    layer_index: int
    issues_to_dispatch: list[int]
    reason: str
    """Human-readable explanation of the decision (great for logs and CLI)."""
    state: OrchestratorState
    """The snapshot the decision was based on."""
    orchestrator_run_id: str = ""
    """UUID for this tick — propagated into the implementer prompt."""
    pollback: PollbackResult = field(default_factory=PollbackResult)
    """Reviewer + merge reconciliation performed at the top of the tick."""
    dispatched: list[DispatchResult] = field(default_factory=list)
    """Successful implementer dispatches (apply mode only)."""
    skipped: list[tuple[int, str]] = field(default_factory=list)
    """``(issue_number, reason)`` pairs for implementer skips."""
    prs_to_review: list[int] = field(default_factory=list)
    """PR numbers the loop identified as needing review this tick."""
    reviewers_dispatched: list[ReviewerDispatchResult] = field(default_factory=list)
    """Successful reviewer dispatches (apply mode only)."""
    reviewers_skipped: list[tuple[int, str]] = field(default_factory=list)
    """``(pr_number, reason)`` pairs for reviewer skips."""

    @property
    def is_idle(self) -> bool:
        """True when nothing should be dispatched this tick (no implementers + no reviewers)."""
        return self.layer_index < 0 and not self.prs_to_review

    @property
    def did_dispatch(self) -> bool:
        """True when at least one agent (impl or reviewer) was actually fired."""
        return bool(self.dispatched) or bool(self.reviewers_dispatched)


def describe_next_action(decision: OrchestratorDecision, settings: Settings) -> str:
    """One-line, human-readable description of the *single* action the next
    apply-mode tick will take — the thing the UI shows as "next action".

    Mirrors the loop's own ordering exactly so the preview never lies:

    1. Paused → nothing happens.
    2. No free slot (in-flight ≥ cap) → the engine waits for the current
       task to finish before doing anything else.
    3. Reviewer pass runs first (it frees slots), so a PR awaiting review
       outranks a fresh implement.
    4. Otherwise the next implementer issue in the active layer.
    5. Nothing ready → idle.

    Pure: derives everything from ``decision`` + ``settings``, no I/O.
    """
    state = decision.state
    cap = state.effective_max_in_flight or settings.concurrency.max_in_flight

    if state.paused:
        return "Paused — no dispatch until resumed"

    if state.active_run_count >= cap:
        suffix = "task" if state.active_run_count == 1 else "tasks"
        return f"Waiting — {state.active_run_count} {suffix} in flight (cap {cap})"

    if decision.prs_to_review:
        return f"Review PR #{decision.prs_to_review[0]}"

    if decision.issues_to_dispatch:
        return (
            f"Implement issue #{decision.issues_to_dispatch[0]} "
            f"(layer {decision.layer_index})"
        )

    return "Idle — nothing ready to dispatch"


def select_prs_needing_review(prs: list[PullRequest]) -> list[PullRequest]:
    """Pure: from a list of open PRs, pick the ones the reviewer hasn't seen.

    A PR needs review iff it has none of:

    - any ``review:*`` terminal label (already reviewed)
    - ``review:pending`` (reviewer already dispatched in a previous tick)
    - ``requires-human-review`` (opt-out, human is on it)

    Order is preserved so the orchestrator picks oldest PRs first.
    """
    skip = REVIEW_TERMINAL_LABELS | {REVIEW_PENDING_LABEL}
    return [pr for pr in prs if not (pr.labels & skip)]


async def _fetch_open_prs(settings: Settings) -> list[PullRequest]:
    """Single source of truth for the open-PR list used in both passes."""
    api_config = GitHubAPIConfig(
        repo=settings.github.repo,
        token=settings.github.token,
    )
    async with GitHubClient(api_config) as gh:
        return await gh.list_open_prs(base="develop")


def _issues_referenced_by_prs(prs: list[PullRequest]) -> frozenset[int]:
    """Issues closed by open PRs — used to guard against double-dispatch."""
    referenced: set[int] = set()
    for pr in prs:
        referenced.update(pr.closing_issues)
    return frozenset(referenced)


async def orchestrator_tick(
    settings: Settings,
    *,
    apply: bool = False,
    notify: bool = True,
) -> OrchestratorDecision:
    """Compute the next decision, optionally executing it.

    Parameters
    ----------
    settings:
        The full engine config.
    apply:
        When False (default), returns the decision without side effects —
        useful for ``orclaw orchestrator tick`` previews and for tests.
        When True, dispatches each issue in the decision via
        :func:`dispatch_implementer` and records the results on the
        returned decision.
    notify:
        When True (default), apply-mode ticks emit observability events:
        Telegram on saturation/errors, Healthchecks.io ping at the end.
        Tests pass ``notify=False`` to keep things hermetic.
    """
    try:
        decision = await _orchestrator_tick_impl(settings, apply=apply)
    except Exception as e:
        if apply and notify:
            await _notify_unexpected_error(settings, error=e)
        raise

    if apply and notify:
        await _notify_observability(settings, decision)
    return decision


async def _orchestrator_tick_impl(
    settings: Settings,
    *,
    apply: bool,
) -> OrchestratorDecision:
    """Pure tick logic — no notification side effects. Wrapped by
    :func:`orchestrator_tick` which adds Telegram + Healthchecks hooks.
    """
    orchestrator_run_id = uuid.uuid4().hex[:12]

    # --- Pollback first: reconcile reviewer verdicts + merged batches --
    # so the subsequent state.load_state() sees fresh data. Only happens
    # in apply mode — preview ticks stay side-effect-free.
    pollback_result = PollbackResult()
    if apply:
        pollback_result = await run_pollback(settings)

    with connect(settings.paths.db_path, read_only=True) as conn:
        state = load_state(conn, settings_max_in_flight=settings.concurrency.max_in_flight)
    # Effective cap: dashboard override (engine_state.max_in_flight_override)
    # wins over settings.concurrency.max_in_flight when set.
    cap_effective = state.effective_max_in_flight or settings.concurrency.max_in_flight

    if state.paused:
        return OrchestratorDecision(
            layer_index=-1,
            issues_to_dispatch=[],
            reason="orchestrator is paused (engine_state.orchestrator_paused = 'true')",
            state=state,
            orchestrator_run_id=orchestrator_run_id,
            pollback=pollback_result,
        )

    if state.batches.total_issues == 0:
        return OrchestratorDecision(
            layer_index=-1,
            issues_to_dispatch=[],
            reason="no batches in DB — run `orclaw planner run` first",
            state=state,
            orchestrator_run_id=orchestrator_run_id,
            pollback=pollback_result,
        )

    # Single GitHub fetch reused by both passes (post-mortem fix from
    # 2026-05-21: never trust the local agent:start label alone).
    open_prs: list[PullRequest]
    try:
        open_prs = await _fetch_open_prs(settings)
    except Exception as e:
        log.warning(
            "orchestrator_pr_lookup_failed",
            error=str(e),
            note="falling back to DB-only view",
        )
        open_prs = []

    issues_with_pr = _issues_referenced_by_prs(open_prs)
    prs_to_review_objs = select_prs_needing_review(open_prs)
    prs_to_review = [pr.number for pr in prs_to_review_objs]

    concurrency_cap = max(0, cap_effective - state.active_run_count)

    # --- Reviewer pass (priority: unblocks auto-merge → frees slots) ---
    reviewers_dispatched: list[ReviewerDispatchResult] = []
    reviewers_skipped: list[tuple[int, str]] = []
    if apply and prs_to_review_objs and concurrency_cap > 0:
        # Reviewers consume cap one at a time.
        slice_for_review = prs_to_review_objs[:concurrency_cap]
        reviewers_dispatched, reviewers_skipped = await _apply_reviewer_dispatches(
            settings=settings,
            prs=slice_for_review,
            orchestrator_run_id=orchestrator_run_id,
        )
        concurrency_cap = max(0, concurrency_cap - len(reviewers_dispatched))

    if concurrency_cap < 1:
        # We may have dispatched reviewers; report that and bail on impl.
        return OrchestratorDecision(
            layer_index=-1,
            issues_to_dispatch=[],
            reason=(
                f"concurrency cap hit after reviewer pass: "
                f"{state.active_run_count + len(reviewers_dispatched)} in flight, "
                f"max {cap_effective}"
            ),
            state=state,
            orchestrator_run_id=orchestrator_run_id,
            pollback=pollback_result,
            prs_to_review=prs_to_review,
            reviewers_dispatched=reviewers_dispatched,
            reviewers_skipped=reviewers_skipped,
        )

    layer_index, issues = next_executable_batch(
        state.batches.layers,
        issues_with_open_pr=issues_with_pr,
        issues_in_progress=state.batches.issues_in_progress,
        concurrency_cap=concurrency_cap,
    )

    if not issues:
        return OrchestratorDecision(
            layer_index=-1,
            issues_to_dispatch=[],
            reason=_idle_reason(prs_to_review, reviewers_dispatched),
            state=state,
            orchestrator_run_id=orchestrator_run_id,
            pollback=pollback_result,
            prs_to_review=prs_to_review,
            reviewers_dispatched=reviewers_dispatched,
            reviewers_skipped=reviewers_skipped,
        )

    if not apply:
        return OrchestratorDecision(
            layer_index=layer_index,
            issues_to_dispatch=issues,
            reason=(
                f"layer {layer_index}: {len(issues)} issue(s) available within cap"
                + (f"; {len(prs_to_review)} PR(s) need review" if prs_to_review else "")
            ),
            state=state,
            orchestrator_run_id=orchestrator_run_id,
            pollback=pollback_result,
            prs_to_review=prs_to_review,
        )

    # --- Implementer pass (apply mode) ---------------------------------
    dispatched, skipped = await _apply_dispatches(
        settings=settings,
        issue_numbers=issues,
        orchestrator_run_id=orchestrator_run_id,
    )
    return OrchestratorDecision(
        layer_index=layer_index,
        issues_to_dispatch=issues,
        reason=(
            f"layer {layer_index}: dispatched {len(dispatched)} of {len(issues)} issue(s)"
            + (f", skipped {len(skipped)}" if skipped else "")
            + (
                f"; reviewer dispatched {len(reviewers_dispatched)}/{len(prs_to_review)}"
                if reviewers_dispatched
                else ""
            )
        ),
        state=state,
        orchestrator_run_id=orchestrator_run_id,
        pollback=pollback_result,
        dispatched=dispatched,
        skipped=skipped,
        prs_to_review=prs_to_review,
        reviewers_dispatched=reviewers_dispatched,
        reviewers_skipped=reviewers_skipped,
    )


def _idle_reason(
    prs_to_review: list[int],
    reviewers_dispatched: list[ReviewerDispatchResult],
) -> str:
    """Pick the friendliest possible idle reason."""
    if reviewers_dispatched:
        return f"reviewer dispatched {len(reviewers_dispatched)}; no implementer slots left"
    if prs_to_review:
        return (
            f"every layer is fully in flight or merged; "
            f"{len(prs_to_review)} PR(s) await reviewer (dispatched next tick)"
        )
    return "every layer is fully in flight or already merged — idling"


async def _apply_reviewer_dispatches(
    *,
    settings: Settings,
    prs: list[PullRequest],
    orchestrator_run_id: str,
) -> tuple[list[ReviewerDispatchResult], list[tuple[int, str]]]:
    """Fan out reviewer dispatches across the selected PRs.

    Same sequential pattern as :func:`_apply_dispatches`. The PRs are
    already filtered by :func:`select_prs_needing_review`, but each
    dispatcher call still does its own label check (belt-and-braces).
    """
    api_config = GitHubAPIConfig(
        repo=settings.github.repo,
        token=settings.github.token,
    )
    dispatched: list[ReviewerDispatchResult] = []
    skipped: list[tuple[int, str]] = []

    async with GitHubClient(api_config) as gh:
        with connect(settings.paths.db_path) as conn:
            for pr in prs:
                try:
                    result = await dispatch_reviewer(
                        conn=conn,
                        gh=gh,
                        pr=pr,
                        orchestrator_run_id=orchestrator_run_id,
                    )
                    dispatched.append(result)
                except DispatchSkippedError as e:
                    log.info("reviewer_dispatch_skipped", pr=pr.number, reason=str(e))
                    skipped.append((pr.number, str(e)))
                except Exception as e:
                    log.error("reviewer_dispatch_failed", pr=pr.number, error=str(e))
                    skipped.append((pr.number, f"error: {e}"))

    return dispatched, skipped


async def _apply_dispatches(
    *,
    settings: Settings,
    issue_numbers: list[int],
    orchestrator_run_id: str,
) -> tuple[list[DispatchResult], list[tuple[int, str]]]:
    """Fan out implementer dispatches across the selected issues.

    Each dispatch is awaited sequentially so we play nicely with GitHub's
    secondary rate limits — the throughput cap is the Pro plan, not the
    REST API.
    """
    api_config = GitHubAPIConfig(
        repo=settings.github.repo,
        token=settings.github.token,
    )
    dispatched: list[DispatchResult] = []
    skipped: list[tuple[int, str]] = []

    async with GitHubClient(api_config) as gh:
        # We open the DB once for the whole batch so the transactions
        # serialise — SQLite is single-writer anyway.
        with connect(settings.paths.db_path) as conn:
            for number in issue_numbers:
                try:
                    issue = await gh.get_issue(number)
                except Exception as e:
                    log.warning(
                        "dispatch_fetch_failed",
                        issue=number,
                        error=str(e),
                    )
                    skipped.append((number, f"fetch failed: {e}"))
                    continue

                try:
                    result = await dispatch_implementer(
                        conn=conn,
                        gh=gh,
                        issue=issue,
                        orchestrator_run_id=orchestrator_run_id,
                    )
                    dispatched.append(result)
                except DispatchSkippedError as e:
                    log.info("dispatch_skipped", issue=number, reason=str(e))
                    skipped.append((number, str(e)))
                except Exception as e:
                    log.error("dispatch_failed", issue=number, error=str(e))
                    skipped.append((number, f"error: {e}"))

    return dispatched, skipped


# =========================================================================
# Observability hooks
# =========================================================================


#: engine_state key recording whether we've already alerted on the current
#: saturation episode. Cleared on the first non-saturated tick.
_SATURATION_FLAG_KEY = "saturation_alert_sent"


def _is_saturation(decision: OrchestratorDecision, settings: Settings) -> bool:
    """A tick is "saturated" iff the cap was hit AND there was useful work
    waiting (pending batches or PRs to review). Pure idle ≠ saturation.
    """
    cap = decision.state.effective_max_in_flight or settings.concurrency.max_in_flight
    if decision.state.active_run_count < cap:
        return False
    has_pending = decision.state.batches.issues_pending or decision.prs_to_review
    return bool(has_pending)


def _alert_already_sent(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM engine_state WHERE key = ?", (_SATURATION_FLAG_KEY,)
    ).fetchone()
    return row is not None and row["value"] == "true"


def _set_alert_sent(conn: sqlite3.Connection, sent: bool) -> None:
    conn.execute(
        "INSERT INTO engine_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (_SATURATION_FLAG_KEY, "true" if sent else "false"),
    )


async def _notify_observability(
    settings: Settings,
    decision: OrchestratorDecision,
) -> None:
    """Telegram + Healthchecks hooks for a completed apply-mode tick.

    - Saturation: post Telegram alert once per episode (de-duped via
      engine_state). Clear the flag on the first non-saturated tick.
    - Healthchecks: ping the orchestrator URL with a short summary so the
      "did the engine tick?" check on Healthchecks.io stays green.
    """
    is_saturated = _is_saturation(decision, settings)

    # --- Saturation de-dup ---
    with connect(settings.paths.db_path) as conn:
        already_alerted = _alert_already_sent(conn)
        if is_saturated and not already_alerted:
            await post_telegram(
                settings.notifications,
                format_saturation_alert(
                    active_runs=decision.state.active_run_count,
                    max_in_flight=decision.state.effective_max_in_flight
                    or settings.concurrency.max_in_flight,
                    pending_batches=len(decision.state.batches.issues_pending),
                    prs_awaiting_review=len(decision.prs_to_review),
                ),
            )
            _set_alert_sent(conn, True)
        elif not is_saturated and already_alerted:
            # Episode ended — clear the flag silently (no recovery message
            # to avoid notification noise).
            _set_alert_sent(conn, False)

    # --- Healthchecks success ping ---
    summary = (
        f"layer={decision.layer_index} "
        f"impl={len(decision.dispatched)} "
        f"reviewers={len(decision.reviewers_dispatched)} "
        f"merged_in_pollback={len(decision.pollback.batches_merged)}"
    )
    await ping_healthchecks(
        settings.notifications.healthchecks_orchestrator_url,
        status="success",
        body=summary,
    )


async def _notify_unexpected_error(
    settings: Settings,
    *,
    error: BaseException,
) -> None:
    """Fire Telegram + Healthchecks failure on uncaught tick errors.

    Best-effort: both helpers swallow their own HTTP errors, so this
    never raises. The original exception is re-raised by the caller.
    """
    await post_telegram(
        settings.notifications,
        format_error_alert(context="orchestrator_tick", error=repr(error)),
    )
    await ping_healthchecks(
        settings.notifications.healthchecks_orchestrator_url,
        status="fail",
        body=f"orchestrator_tick raised: {error!r}",
    )
