"""Configuration loading.

Two layers:

1. **TOML files** for non-sensitive defaults and caps. Looked up in:
   - ``$ORCLAW_CONFIG_DIR`` if set
   - ``/etc/orclaw/`` (server)
   - ``./config/`` (dev, alongside the source tree)

2. **Environment variables** for secrets and per-host overrides. Loaded
   from ``/etc/orclaw/secrets.env`` if present; otherwise from the
   current process environment.

The combined view is exposed as a frozen ``Settings`` dataclass.
"""

from __future__ import annotations

import os
import tomllib  # Python 3.11+ stdlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orclaw.exceptions import ConfigError, MissingSecretError

# --- Path resolution -------------------------------------------------------


def _config_search_paths() -> list[Path]:
    """Where to look for TOML config, in priority order."""
    env_dir = os.environ.get("ORCLAW_CONFIG_DIR")
    candidates = [
        Path(env_dir) if env_dir else None,
        Path("/etc/orclaw"),
        Path(__file__).parent.parent / "config",
    ]
    return [p for p in candidates if p is not None]


def _find_toml(name: str) -> Path | None:
    for base in _config_search_paths():
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def _load_toml(name: str, required: bool = False) -> dict[str, Any]:
    path = _find_toml(name)
    if path is None:
        if required:
            raise ConfigError(
                f"Required config file '{name}' not found in any of: "
                + ", ".join(str(p) for p in _config_search_paths())
            )
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# --- Settings dataclasses --------------------------------------------------


@dataclass(frozen=True)
class GitHubSettings:
    repo: str = ""
    """Target repo for engine actions (`owner/name`). Set via
    ``ORCLAW_GITHUB_REPO`` env var."""
    token: str = ""
    """GitHub Personal Access Token with ``repo`` scope. From env
    (``GITHUB_TOKEN``)."""
    poll_interval_seconds: int = 30
    """How often the orchestrator polls GitHub for events."""

    def __post_init__(self) -> None:
        # Empty is allowed at construction time — the dashboard / specialist
        # can be imported on a freshly-installed box before the wizard runs.
        # The orchestrator + planner will refuse to start until you set
        # ORCLAW_GITHUB_REPO via `orclaw doctor`.
        if self.repo and "/" not in self.repo:
            raise ConfigError(f"github.repo must be 'owner/name', got: {self.repo!r}")


@dataclass(frozen=True)
class ConcurrencySettings:
    max_in_flight: int = 2
    """Hard cap on simultaneous @claude mentions awaiting completion."""
    default_in_flight: int = 1
    """Default cap when Pro quota is observed healthy."""

    def __post_init__(self) -> None:
        if self.default_in_flight > self.max_in_flight:
            raise ConfigError(
                f"default_in_flight ({self.default_in_flight}) cannot exceed "
                f"max_in_flight ({self.max_in_flight})"
            )


@dataclass(frozen=True)
class BackoffSettings:
    saturation_threshold_failures: int = 2
    saturation_window_minutes: int = 10
    saturation_cooldown_minutes: int = 30
    retry_initial_seconds: int = 60
    retry_max_attempts: int = 3
    retry_max_total_minutes: int = 30


@dataclass(frozen=True)
class NotificationsSettings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    healthchecks_orchestrator_url: str = ""
    healthchecks_planner_url: str = ""
    healthchecks_backup_url: str = ""
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@dataclass(frozen=True)
class PathsSettings:
    data_dir: Path = Path("/var/lib/orclaw/data")
    mirror_dir: Path = Path("/var/lib/orclaw/target-repo-mirror")
    worktrees_dir: Path = Path("/var/lib/orclaw/worktrees")
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        # Use object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "db_path", self.data_dir / "engine.db")


@dataclass(frozen=True)
class Settings:
    """Top-level frozen settings view used by the engine."""

    github: GitHubSettings = field(default_factory=GitHubSettings)
    concurrency: ConcurrencySettings = field(default_factory=ConcurrencySettings)
    backoff: BackoffSettings = field(default_factory=BackoffSettings)
    notifications: NotificationsSettings = field(default_factory=NotificationsSettings)
    paths: PathsSettings = field(default_factory=PathsSettings)
    log_level: str = "INFO"


# --- Loading ---------------------------------------------------------------


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise MissingSecretError(
            f"Required environment variable '{key}' is not set. "
            f"Set it in /etc/orclaw/secrets.env or the process environment."
        )
    return val


def load_settings(
    *,
    require_secrets: bool = True,
    data_dir_override: Path | None = None,
) -> Settings:
    """Build the Settings view from disk + environment.

    Parameters
    ----------
    require_secrets
        If True, missing secrets raise :class:`MissingSecretError`. Set to
        False for tests / dry-runs that don't actually talk to GitHub.
    data_dir_override
        For tests: redirect the data directory to a tmp path.
    """
    budget_toml = _load_toml("budget.toml")
    _load_toml("notifications.toml")

    concurrency_raw = budget_toml.get("concurrency", {})
    backoff_raw = budget_toml.get("backoff", {})

    github_token = _env("GITHUB_TOKEN")
    if require_secrets and not github_token:
        raise MissingSecretError(
            "GITHUB_TOKEN is required. It is the PAT used to impersonate the "
            "CEO/CTO when posting @claude comments. See docs/deployment.md."
        )

    # Resolution order for data_dir:
    #   1. explicit data_dir_override (tests)
    #   2. ORCLAW_DATA_DIR env (set by install wizard / docker / cloud-init)
    #   3. PathsSettings() default (/var/lib/orclaw/data)
    env_data_dir = _env("ORCLAW_DATA_DIR")
    paths = PathsSettings(
        data_dir=data_dir_override or (Path(env_data_dir) if env_data_dir else PathsSettings().data_dir),
    )

    return Settings(
        github=GitHubSettings(
            repo=_env("GITHUB_REPO", ""),
            token=github_token,
            poll_interval_seconds=int(_env("GITHUB_POLL_INTERVAL", "30")),
        ),
        concurrency=ConcurrencySettings(
            max_in_flight=int(concurrency_raw.get("max_in_flight", 2)),
            default_in_flight=int(concurrency_raw.get("default_in_flight", 1)),
        ),
        backoff=BackoffSettings(
            saturation_threshold_failures=int(backoff_raw.get("saturation_threshold_failures", 2)),
            saturation_window_minutes=int(backoff_raw.get("saturation_window_minutes", 10)),
            saturation_cooldown_minutes=int(backoff_raw.get("saturation_cooldown_minutes", 30)),
            retry_initial_seconds=int(backoff_raw.get("retry_initial_seconds", 60)),
            retry_max_attempts=int(backoff_raw.get("retry_max_attempts", 3)),
            retry_max_total_minutes=int(backoff_raw.get("retry_max_total_minutes", 30)),
        ),
        notifications=NotificationsSettings(
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            healthchecks_orchestrator_url=_env("HEALTHCHECKS_ORCHESTRATOR_URL"),
            healthchecks_planner_url=_env("HEALTHCHECKS_PLANNER_URL"),
            healthchecks_backup_url=_env("HEALTHCHECKS_BACKUP_URL"),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
            discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
        ),
        paths=paths,
        log_level=_env("ORCLAW_LOG_LEVEL", "INFO"),
    )
