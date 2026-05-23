"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from orclaw.config import (
    BackoffSettings,
    ConcurrencySettings,
    GitHubSettings,
    NotificationsSettings,
    PathsSettings,
    Settings,
)
from orclaw.models import Issue

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """Fresh data dir + db parent for each test."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def settings(tmp_data_dir: Path) -> Settings:
    """Test Settings without secrets enforcement."""
    paths = PathsSettings(data_dir=tmp_data_dir)
    return Settings(
        github=GitHubSettings(token="ghp_test_token"),
        concurrency=ConcurrencySettings(),
        backoff=BackoffSettings(),
        notifications=NotificationsSettings(),
        paths=paths,
    )


@pytest.fixture()
def issue_factory():
    """Build an Issue with sensible defaults for tests."""

    def _make(
        *,
        number: int = 1,
        title: str = "test issue",
        body: str = "",
        state: str = "open",
        labels: tuple[str, ...] = (),
    ) -> Issue:
        now = datetime(2026, 5, 22, tzinfo=UTC)
        return Issue(
            number=number,
            title=title,
            body=body,
            state=state,
            labels=frozenset(labels),
            created_at=now,
            updated_at=now,
            closed_at=None if state == "open" else now,
        )

    return _make
