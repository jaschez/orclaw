"""Tests for GitHub webhook ingestion helpers (``orclaw.webhooks``).

Pure helpers (signature verification + event relevance) plus the
coalescing tick runner. No HTTP here — the endpoint is covered in
test_dashboard.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from orclaw.webhooks import (
    CoalescingTickRunner,
    event_should_trigger,
    verify_signature,
)

_SECRET = "s3cr3t"


def _sign(secret: str, payload: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_valid_signature_passes(self) -> None:
        body = b'{"hello": "world"}'
        assert verify_signature(_SECRET, body, _sign(_SECRET, body)) is True

    def test_wrong_secret_fails(self) -> None:
        body = b'{"hello": "world"}'
        assert verify_signature(_SECRET, body, _sign("other", body)) is False

    def test_tampered_body_fails(self) -> None:
        sig = _sign(_SECRET, b'{"hello": "world"}')
        assert verify_signature(_SECRET, b'{"hello": "evil"}', sig) is False

    def test_missing_header_fails(self) -> None:
        assert verify_signature(_SECRET, b"{}", None) is False

    def test_empty_secret_fails(self) -> None:
        body = b"{}"
        assert verify_signature("", body, _sign(_SECRET, body)) is False

    def test_non_sha256_prefix_fails(self) -> None:
        assert verify_signature(_SECRET, b"{}", "sha1=deadbeef") is False


class TestEventShouldTrigger:
    @pytest.mark.parametrize(
        ("event", "action"),
        [
            ("issue_comment", "created"),
            ("pull_request", "opened"),
            ("pull_request", "synchronize"),
            ("pull_request_review", "submitted"),
            ("workflow_run", "completed"),
            ("issues", "labeled"),
        ],
    )
    def test_relevant_events_trigger(self, event: str, action: str) -> None:
        assert event_should_trigger(event, {"action": action}) is True

    def test_push_triggers_without_action(self) -> None:
        assert event_should_trigger("push", {"ref": "refs/heads/main"}) is True

    def test_irrelevant_action_ignored(self) -> None:
        # e.g. a PR comment "assigned" event isn't worth a tick.
        assert event_should_trigger("pull_request", {"action": "assigned"}) is False

    def test_unknown_event_ignored(self) -> None:
        assert event_should_trigger("star", {"action": "created"}) is False

    def test_ping_is_not_a_trigger(self) -> None:
        # ping is handled separately (pong) by the endpoint.
        assert event_should_trigger("ping", {}) is False


class TestCoalescingTickRunner:
    def test_runs_when_idle(self) -> None:
        calls = 0

        async def runner() -> None:
            nonlocal calls
            calls += 1

        async def go() -> None:
            r = CoalescingTickRunner(runner)
            result = await r.trigger()
            assert result == "ran"

        asyncio.run(go())
        assert calls == 1

    def test_coalesces_burst_into_trailing_run(self) -> None:
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Hold the first run open so concurrent triggers pile up.
                started.set()
                await release.wait()

        async def go() -> None:
            r = CoalescingTickRunner(runner)
            first = asyncio.create_task(r.trigger())
            await started.wait()
            # Three deliveries arrive while the first tick is in flight.
            second = await r.trigger()
            third = await r.trigger()
            assert second == "coalesced"
            assert third == "coalesced"
            release.set()
            assert await first == "ran"

        asyncio.run(go())
        # 1 initial + exactly 1 trailing run for the whole burst.
        assert calls == 2

    def test_runner_exception_does_not_propagate(self) -> None:
        async def boom() -> None:
            raise RuntimeError("kaboom")

        async def go() -> None:
            r = CoalescingTickRunner(boom)
            # Must not raise — failures are logged, not surfaced.
            assert await r.trigger() == "ran"

        asyncio.run(go())
