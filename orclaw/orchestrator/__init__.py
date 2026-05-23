"""Orchestrator — long-running coordinator on the server.

The orchestrator decides *what to do next* and posts ``@claude`` comments
to drive Claude actions in the ``${TARGET_REPO}`` repo. In sprint 2 the loop
is **dry-run only**: it computes decisions and writes them to logs but
does not actually post comments. Sprint 3 wires the @claude posting.

See ``docs/master-plan.md`` § 3.2 for the end-to-end flow.
"""

from __future__ import annotations

from orclaw.orchestrator.dispatcher import (
    DispatchResult,
    DispatchSkippedError,
    ReviewerDispatchResult,
    dispatch_implementer,
    dispatch_reviewer,
    render_implementer_prompt,
    render_reviewer_prompt,
)
from orclaw.orchestrator.loop import OrchestratorDecision, orchestrator_tick
from orclaw.orchestrator.pollback import PollbackResult, run_pollback
from orclaw.orchestrator.state import (
    BatchSnapshot,
    OrchestratorState,
    create_run,
    effective_max_in_flight,
    get_concurrency_override,
    is_paused,
    issues_in_progress,
    load_state,
    set_concurrency_override,
    set_paused,
)

__all__ = [
    "BatchSnapshot",
    "DispatchResult",
    "DispatchSkippedError",
    "OrchestratorDecision",
    "OrchestratorState",
    "PollbackResult",
    "ReviewerDispatchResult",
    "create_run",
    "dispatch_implementer",
    "dispatch_reviewer",
    "effective_max_in_flight",
    "get_concurrency_override",
    "is_paused",
    "issues_in_progress",
    "load_state",
    "orchestrator_tick",
    "render_implementer_prompt",
    "render_reviewer_prompt",
    "run_pollback",
    "set_concurrency_override",
    "set_paused",
]
