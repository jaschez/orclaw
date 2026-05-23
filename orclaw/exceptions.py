"""Domain-specific exceptions for orclaw.

We define a small hierarchy so callers can catch broad categories without
having to import every concrete error type.
"""

from __future__ import annotations


class OrclawError(Exception):
    """Base class for every exception raised by this package."""


# --- Configuration ---------------------------------------------------------


class ConfigError(OrclawError):
    """A configuration file is missing, malformed, or contradicts itself."""


class MissingSecretError(ConfigError):
    """A required environment-defined secret is not set."""


# --- Database --------------------------------------------------------------


class DatabaseError(OrclawError):
    """Generic SQLite-layer error."""


class SchemaMismatchError(DatabaseError):
    """The on-disk schema does not match what the code expects."""


# --- GitHub ----------------------------------------------------------------


class GitHubError(OrclawError):
    """Generic upstream GitHub error."""


class RateLimitedError(GitHubError):
    """GitHub said we are being rate-limited."""


class GitHubAuthError(GitHubError):
    """The provided token is missing scopes or invalid."""


# --- Dependency graph ------------------------------------------------------


class DependencyCycleError(OrclawError):
    """The dependency graph derived from "Blocked by #N" contains a cycle."""

    def __init__(self, issues_in_cycle: list[int]) -> None:
        self.issues_in_cycle = issues_in_cycle
        super().__init__(f"Dependency cycle detected involving issues: {issues_in_cycle}")
