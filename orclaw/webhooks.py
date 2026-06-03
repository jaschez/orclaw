"""GitHub webhook ingestion — push instead of poll.

The orchestrator historically learned about new issues, comments, PRs and
workflow results by **polling** GitHub every N seconds (a systemd timer
firing ``orclaw orchestrator tick``). Polling is simple but laggy and
chatty, and it scales badly as you add repos.

This module lets the running dashboard (a FastAPI app already behind
Cloudflare) **receive** GitHub webhook deliveries and react immediately,
while polling stays as a cheap reconciliation fallback for any delivery
that gets dropped.

Two pure helpers (unit-testable, no I/O):

- :func:`verify_signature` — validate the ``X-Hub-Signature-256`` HMAC.
- :func:`event_should_trigger` — decide whether a delivery is worth a tick.

Plus :class:`CoalescingTickRunner`, which serialises apply-mode ticks so a
burst of deliveries collapses into "run now, then run once more if
anything arrived while we were busy" rather than stampeding GitHub.

Security note: the ``/webhook/github`` route must be **excluded from
Cloudflare Access** (GitHub can't present an Access cookie). The HMAC
signature is the authentication for that path — never trust a delivery
whose signature does not verify.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import TYPE_CHECKING, Any

from orclaw.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)


# --- Signature verification ------------------------------------------------


_SIGNATURE_PREFIX = "sha256="


def verify_signature(
    secret: str,
    payload: bytes,
    signature_header: str | None,
) -> bool:
    """Validate a GitHub ``X-Hub-Signature-256`` header against ``payload``.

    Returns ``True`` only when:

    - a non-empty ``secret`` is configured, and
    - ``signature_header`` is present and shaped ``sha256=<hex>``, and
    - the HMAC-SHA256 of the raw ``payload`` under ``secret`` matches,
      compared in constant time.

    Any deviation returns ``False`` — callers should answer 401. Empty
    secret returns ``False`` too: a misconfigured server must not silently
    accept unsigned traffic.
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = (
        _SIGNATURE_PREFIX
        + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


# --- Event relevance -------------------------------------------------------


#: For each webhook event type we care about, the set of ``action`` values
#: worth a tick. ``None`` means "any action / no action field" (e.g. push).
#: Everything not listed here is ignored (return 200, do nothing) so we
#: don't burn a Pro-plan slot reacting to, say, a label colour change on a
#: wiki page.
_TRIGGER_ACTIONS: dict[str, frozenset[str] | None] = {
    # A human (or the engine) mentioned @claude, or edited a comment.
    "issue_comment": frozenset({"created", "edited"}),
    "pull_request_review_comment": frozenset({"created", "edited"}),
    # PR lifecycle that changes what the orchestrator should do next.
    "pull_request": frozenset(
        {
            "opened",
            "reopened",
            "synchronize",
            "closed",
            "labeled",
            "unlabeled",
            "ready_for_review",
        }
    ),
    # Reviewer verdicts land as reviews.
    "pull_request_review": frozenset({"submitted", "dismissed"}),
    # CI finished → maybe a PR is now mergeable / a run completed.
    "workflow_run": frozenset({"completed"}),
    # New / changed issues feed the planner's next pass.
    "issues": frozenset({"opened", "reopened", "edited", "labeled", "closed"}),
    # A merge to a branch may close issues / free a layer.
    "push": None,
}


def event_should_trigger(event: str, payload: dict[str, Any]) -> bool:
    """Decide whether a webhook delivery warrants an orchestrator tick.

    ``event`` is the ``X-GitHub-Event`` header value; ``payload`` the parsed
    JSON body. ``ping`` is handled by the caller (it answers pong) and is
    intentionally not a trigger here.
    """
    if event not in _TRIGGER_ACTIONS:
        return False
    actions = _TRIGGER_ACTIONS[event]
    if actions is None:
        return True
    action = payload.get("action")
    return isinstance(action, str) and action in actions


# --- Coalescing tick runner ------------------------------------------------


class CoalescingTickRunner:
    """Serialise apply-mode ticks and collapse bursts into a trailing run.

    GitHub can deliver a flurry of webhooks in a second (open PR →
    synchronize → CI run → review). Running an apply tick per delivery
    would hammer GitHub and the Pro plan. Instead:

    - If no tick is running, run one now.
    - If a tick is already running, remember that "more happened" and run
      exactly one more after the current one finishes — no matter how many
      deliveries arrived in the meantime.

    This is best-effort: the (rare) race where a delivery lands in the
    microscopic window between the holder's final pending-check and lock
    release is covered by the polling fallback. The runner never raises to
    the caller — failures are logged.
    """

    def __init__(self, runner: Callable[[], Awaitable[None]]) -> None:
        self._runner = runner
        self._lock = asyncio.Lock()
        self._pending = False

    async def trigger(self) -> str:
        """Run (or coalesce into) an apply tick.

        Returns ``"ran"`` if this call drove at least one tick, or
        ``"coalesced"`` if a tick was already in flight and this delivery
        was folded into the trailing run.
        """
        if self._lock.locked():
            self._pending = True
            return "coalesced"

        async with self._lock:
            await self._run_once()
            while self._pending:
                self._pending = False
                await self._run_once()
        return "ran"

    async def _run_once(self) -> None:
        try:
            await self._runner()
        except Exception as e:  # never let a bad tick kill the webhook loop
            log.error("webhook_tick_failed", error=str(e))
