"""Tests for the GitHub client (httpx mocking)."""

from __future__ import annotations

# httpx mock support — install via dev dep ``pytest-httpx`` (added separately if needed).
# For sprint 1 we keep it simple and only verify parsing logic on canned payloads,
# without actually exercising the AsyncClient. Full network mocking is sprint 2+.
from orclaw.github_client import GitHubClient


class TestIssueFromPayload:
    """Verify the static parser handles GitHub API shapes correctly."""

    def test_parses_minimum_fields(self) -> None:
        payload = {
            "number": 88,
            "title": "schema migration",
            "body": "Body here.",
            "state": "open",
            "labels": [{"name": "P0"}, {"name": "block:B0"}],
            "created_at": "2026-05-18T20:00:00Z",
            "updated_at": "2026-05-22T10:00:00Z",
            "closed_at": None,
        }
        issue = GitHubClient._issue_from_payload(payload)
        assert issue.number == 88
        assert issue.title == "schema migration"
        assert issue.body == "Body here."
        assert "P0" in issue.labels
        assert "block:B0" in issue.labels
        assert issue.is_open is True
        assert issue.is_ops is False

    def test_handles_null_body(self) -> None:
        payload = {
            "number": 1,
            "title": "x",
            "body": None,
            "state": "open",
            "labels": [],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T10:00:00Z",
        }
        issue = GitHubClient._issue_from_payload(payload)
        assert issue.body == ""

    def test_recognises_ops_label(self) -> None:
        payload = {
            "number": 1,
            "title": "Apply migration",
            "body": "",
            "state": "open",
            "labels": [{"name": "ops"}, {"name": "P0"}],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T10:00:00Z",
        }
        issue = GitHubClient._issue_from_payload(payload)
        assert issue.is_ops is True

    def test_recognises_ready_label(self) -> None:
        payload = {
            "number": 1,
            "title": "Ready",
            "body": "",
            "state": "open",
            "labels": [{"name": "agent:ready"}],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T10:00:00Z",
        }
        issue = GitHubClient._issue_from_payload(payload)
        assert issue.is_ready is True
        assert issue.is_in_progress is False


class TestPRFromPayload:
    def test_parses_minimum_fields(self) -> None:
        payload = {
            "number": 142,
            "title": "feat(coupons): add",
            "body": "Closes #88",
            "head": {"ref": "feat/142-coupons"},
            "base": {"ref": "develop"},
            "state": "open",
            "merged": False,
            "labels": [{"name": "auto-merge"}],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T11:00:00Z",
            "merged_at": None,
        }
        pr = GitHubClient._pr_from_payload(payload, closing_issues=frozenset({88}))
        assert pr.number == 142
        assert pr.head_ref == "feat/142-coupons"
        assert pr.base_ref == "develop"
        assert pr.merged is False
        assert 88 in pr.closing_issues
        assert "auto-merge" in pr.labels

    def test_derives_merged_from_merged_at_when_field_missing(self) -> None:
        """Regression: GitHub's /pulls LIST endpoint doesn't include the
        ``merged`` field — only the single-PR endpoint does. We must derive
        it from ``merged_at`` or the pollback merged-batch reconciliation
        silently does nothing on every tick. Found in prod on first deploy.
        """
        # No "merged" key — exactly what the list endpoint returns.
        payload = {
            "number": 191,
            "title": "feat: add events table",
            "body": "Closes #110",
            "head": {"ref": "feat/110-events"},
            "base": {"ref": "develop"},
            "state": "closed",
            "labels": [{"name": "auto-merge"}, {"name": "review:approved"}],
            "created_at": "2026-05-23T14:00:00Z",
            "updated_at": "2026-05-23T15:09:19Z",
            "merged_at": "2026-05-23T15:08:55Z",  # PR was merged
        }
        pr = GitHubClient._pr_from_payload(payload, closing_issues=frozenset({110}))
        assert pr.merged is True
        assert pr.merged_at is not None

    def test_unmerged_closed_pr_has_no_merged_at(self) -> None:
        # Closed-but-not-merged (manual close): no merged_at, no merged key.
        payload = {
            "number": 192,
            "title": "abandoned",
            "body": "",
            "head": {"ref": "feat/abandoned"},
            "base": {"ref": "develop"},
            "state": "closed",
            "labels": [],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T11:00:00Z",
            "merged_at": None,
        }
        pr = GitHubClient._pr_from_payload(payload, closing_issues=frozenset())
        assert pr.merged is False

    def test_explicit_merged_field_wins(self) -> None:
        # Single-PR endpoint returns merged explicitly; honor it.
        payload = {
            "number": 1,
            "title": "x",
            "body": "",
            "head": {"ref": "x"},
            "base": {"ref": "develop"},
            "state": "closed",
            "merged": True,  # explicit
            "labels": [],
            "created_at": "2026-05-22T10:00:00Z",
            "updated_at": "2026-05-22T11:00:00Z",
            "merged_at": "2026-05-22T11:00:00Z",
        }
        pr = GitHubClient._pr_from_payload(payload, closing_issues=frozenset())
        assert pr.merged is True


class TestNextLinkParser:
    def test_returns_url_for_rel_next(self) -> None:
        import httpx

        resp = httpx.Response(
            200,
            headers={
                "link": (
                    '<https://api.github.com/repositories/1/issues?page=2>; rel="next", '
                    '<https://api.github.com/repositories/1/issues?page=10>; rel="last"'
                )
            },
        )
        assert (
            GitHubClient._next_link(resp) == "https://api.github.com/repositories/1/issues?page=2"
        )

    def test_returns_none_when_no_next(self) -> None:
        import httpx

        resp = httpx.Response(200, headers={"link": '<...>; rel="last"'})
        assert GitHubClient._next_link(resp) is None

    def test_returns_none_when_no_link_header(self) -> None:
        import httpx

        resp = httpx.Response(200)
        assert GitHubClient._next_link(resp) is None
