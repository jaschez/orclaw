"""Domain models used across the engine.

These are immutable dataclasses (frozen) that the rest of the engine passes
around. They map roughly 1:1 to rows in the SQLite schema and to GitHub
issue/PR objects, but stay simple — no ORM, no magic, no opinions about
how they are persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

# --- Enums -----------------------------------------------------------------


class BatchStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    RATE_LIMITED = "rate_limited"


class AgentKind(StrEnum):
    SPECIALIST = "specialist"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    BATCH_PLANNER = "batch_planner"


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    MINOR_FIXES_APPLIED = "minor_fixes_applied"
    NEEDS_CHANGES = "needs_changes"
    HARD_BLOCK = "hard_block"


# --- GitHub-shaped types ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Issue:
    """A GitHub issue (the engine only consumes these, never creates them
    server-side; the specialist agent creates issues via @claude)."""

    number: int
    title: str
    body: str
    state: str  # 'open' | 'closed'
    labels: frozenset[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_ops(self) -> bool:
        return "ops" in self.labels

    @property
    def is_ready(self) -> bool:
        return "agent:ready" in self.labels

    @property
    def is_in_progress(self) -> bool:
        return "agent:start" in self.labels


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A GitHub PR. We track only the fields the orchestrator cares about."""

    number: int
    title: str
    body: str
    head_ref: str
    base_ref: str
    state: str  # 'open' | 'closed'
    merged: bool
    labels: frozenset[str]
    closing_issues: frozenset[int]  # via GraphQL closingIssuesReferences
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None


# --- Engine internal state -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Batch:
    """A row from the ``batches`` SQLite table."""

    id: int
    layer: int
    issue_number: int
    status: BatchStatus
    implementer_run_id: str | None
    pr_number: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Run:
    """A row from the ``runs`` SQLite table."""

    id: str
    agent: AgentKind
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    model: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: int | None
    exit_code: int | None
    notes: str | None


# --- Composed views (built ad hoc, not persisted) -------------------------


@dataclass(frozen=True, slots=True)
class IssueDependencies:
    """The result of parsing 'Blocked by #N' from an issue body."""

    issue_number: int
    blocked_by: frozenset[int] = field(default_factory=frozenset)

    @property
    def is_root(self) -> bool:
        """A root issue has no open dependencies."""
        return not self.blocked_by
