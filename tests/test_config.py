"""Tests for config loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orclaw.config import (
    ConcurrencySettings,
    GitHubSettings,
    load_settings,
)
from orclaw.exceptions import ConfigError, MissingSecretError

if TYPE_CHECKING:
    from pathlib import Path


class TestGitHubSettings:
    def test_validates_repo_format(self) -> None:
        with pytest.raises(ConfigError, match="must be 'owner/name'"):
            GitHubSettings(repo="just-a-name")

    def test_accepts_valid_repo(self) -> None:
        gh = GitHubSettings(repo="owner/name", token="ghp_test")
        assert gh.repo == "owner/name"


class TestConcurrencySettings:
    def test_default_cannot_exceed_max(self) -> None:
        with pytest.raises(ConfigError, match="cannot exceed"):
            ConcurrencySettings(default_in_flight=5, max_in_flight=2)


class TestLoadSettings:
    def test_requires_github_token_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(MissingSecretError, match="GITHUB_TOKEN"):
            load_settings(require_secrets=True)

    def test_skips_secret_check_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        settings = load_settings(require_secrets=False)
        assert settings.github.token == ""

    def test_picks_up_env_values(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
        monkeypatch.setenv("GITHUB_REPO", "example/target")
        monkeypatch.setenv("GITHUB_POLL_INTERVAL", "45")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "789")

        # No TOML files in tmp dir
        monkeypatch.setenv("ORCLAW_CONFIG_DIR", str(tmp_path))

        settings = load_settings(require_secrets=True)
        assert settings.github.token == "ghp_from_env"
        assert settings.github.poll_interval_seconds == 45
        assert settings.notifications.telegram_enabled is True

    def test_telegram_enabled_only_when_both_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("ORCLAW_CONFIG_DIR", str(tmp_path))

        settings = load_settings()
        assert settings.notifications.telegram_enabled is False
