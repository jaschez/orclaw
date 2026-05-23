"""Tests for the outbound notifications module.

We use ``httpx.MockTransport`` so no real HTTP traffic happens. The
asserts focus on (1) "is this a no-op when settings are empty?" and
(2) "does it post the right URL + payload when configured?".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

import orclaw.notifications as nf
from orclaw.config import NotificationsSettings
from orclaw.notifications import (
    format_error_alert,
    format_saturation_alert,
    ping_healthchecks,
    post_telegram,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# --- helpers ---------------------------------------------------------------


@pytest.fixture()
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """Patch the ``AsyncClient`` symbol in ``orclaw.notifications`` so it
    returns a client backed by :class:`httpx.MockTransport`.

    Returns a setter: tests call ``setter(handler)`` to install a request
    handler. The returned list collects every request the code under test
    issued (regardless of handler).
    """
    captured: list[httpx.Request] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> list[httpx.Request]:
        def default_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok")

        def tracking_handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return (handler or default_handler)(req)

        transport = httpx.MockTransport(tracking_handler)
        real_async_client = httpx.AsyncClient  # capture original before patch

        def factory_fn(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("timeout", None)
            return real_async_client(transport=transport)

        monkeypatch.setattr(nf.httpx, "AsyncClient", factory_fn)
        return captured

    return factory


# --- post_telegram ---------------------------------------------------------


class TestPostTelegram:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, mock_http: Callable[..., list[httpx.Request]]) -> None:
        captured = mock_http()  # default handler, but should never be called
        notif = NotificationsSettings()  # empty → disabled

        sent = await post_telegram(notif, "hello")
        assert sent is False
        assert captured == []

    @pytest.mark.asyncio
    async def test_posts_to_telegram_api(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        captured = mock_http()
        notif = NotificationsSettings(
            telegram_bot_token="abc:def",
            telegram_chat_id="42",
        )

        sent = await post_telegram(notif, "hello")
        assert sent is True
        assert len(captured) == 1
        req = captured[0]
        assert "api.telegram.org/botabc:def/sendMessage" in str(req.url)

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"boom")

        mock_http(handler)
        notif = NotificationsSettings(telegram_bot_token="abc", telegram_chat_id="42")

        sent = await post_telegram(notif, "hello")
        assert sent is False

    @pytest.mark.asyncio
    async def test_truncates_long_messages(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, content=b"ok")

        mock_http(handler)
        notif = NotificationsSettings(telegram_bot_token="abc", telegram_chat_id="42")
        long_text = "x" * 5000

        sent = await post_telegram(notif, long_text)
        assert sent is True
        # Body should be truncated under the 4096 Telegram limit.
        import json

        body = json.loads(captured[0].content)
        assert len(body["text"]) <= 4096
        assert "(truncated)" in body["text"]


# --- ping_healthchecks -----------------------------------------------------


class TestPingHealthchecks:
    @pytest.mark.asyncio
    async def test_noop_when_url_empty(self, mock_http: Callable[..., list[httpx.Request]]) -> None:
        captured = mock_http()
        sent = await ping_healthchecks("")
        assert sent is False
        assert captured == []

    @pytest.mark.asyncio
    async def test_success_pings_bare_url(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        captured = mock_http()
        sent = await ping_healthchecks("https://hc-ping.com/abc-def")
        assert sent is True
        assert str(captured[0].url) == "https://hc-ping.com/abc-def"

    @pytest.mark.asyncio
    async def test_fail_pings_fail_suffix(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        captured = mock_http()
        sent = await ping_healthchecks("https://hc-ping.com/abc-def", status="fail")
        assert sent is True
        assert str(captured[0].url).endswith("/fail")

    @pytest.mark.asyncio
    async def test_start_pings_start_suffix(
        self, mock_http: Callable[..., list[httpx.Request]]
    ) -> None:
        captured = mock_http()
        await ping_healthchecks("https://hc-ping.com/abc-def", status="start")
        assert str(captured[0].url).endswith("/start")


# --- formatters (pure) -----------------------------------------------------


class TestFormatters:
    def test_saturation_alert_includes_numbers(self) -> None:
        msg = format_saturation_alert(
            active_runs=2, max_in_flight=2, pending_batches=5, prs_awaiting_review=3
        )
        assert "2/2" in msg
        assert "Pending batches: `5`" in msg
        assert "PRs awaiting review: `3`" in msg

    def test_error_alert_truncates_long_error(self) -> None:
        msg = format_error_alert(context="tick", error="x" * 5000)
        assert len(msg) < 5000  # truncated under telegram limit-ish
        assert "tick" in msg
