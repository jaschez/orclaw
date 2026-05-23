"""GitHub client.

A thin async wrapper around the GitHub REST + GraphQL APIs using the PAT
configured in :class:`GitHubSettings`. We deliberately keep this small —
it covers only the surface the orchestrator actually needs.

The PAT acts on behalf of the CEO/CTO (jaschez) so comments posted via
:meth:`post_comment` appear as their authorship. This is the
"impersonation" pattern described in ``docs/pro-plan-strategy.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from orclaw.exceptions import GitHubAuthError, GitHubError, RateLimitedError
from orclaw.logging import get_logger
from orclaw.models import Issue, PullRequest

log = get_logger(__name__)


# --- DTO for the underlying clients ----------------------------------------


@dataclass(frozen=True)
class GitHubAPIConfig:
    repo: str
    token: str
    base_url: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"
    timeout_seconds: float = 30.0


# --- Helpers ---------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string from the GitHub API."""
    if not value:
        return None
    # GH uses 'Z' suffix; Python's fromisoformat handles this in 3.11+.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _check_response(resp: httpx.Response) -> None:
    """Raise the right exception class based on the status code."""
    if resp.status_code == 401:
        raise GitHubAuthError("GitHub returned 401. Token is invalid or expired.")
    if resp.status_code == 403:
        # Could be rate limit, or missing scope.
        if "rate limit" in resp.text.lower() or resp.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimitedError(
                f"GitHub rate limit hit. Reset at "
                f"{resp.headers.get('x-ratelimit-reset', 'unknown')}."
            )
        raise GitHubAuthError(f"GitHub 403: probably missing PAT scope. Body: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub {resp.status_code}: {resp.text[:500]}")


# --- Client ----------------------------------------------------------------


class GitHubClient:
    """Async GitHub client.

    Use as a context manager so the underlying HTTP client is closed cleanly::

        async with GitHubClient(config) as gh:
            issue = await gh.get_issue(116)
    """

    def __init__(self, config: GitHubAPIConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GitHubClient:
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=self._config.timeout_seconds,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._config.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "orclaw/0.1",
            },
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise GitHubError("GitHubClient used outside its async context manager.")
        return self._client

    # --- Issues ------------------------------------------------------------

    async def get_issue(self, number: int) -> Issue:
        """Fetch a single issue by number."""
        resp = await self._http.get(f"/repos/{self._config.repo}/issues/{number}")
        _check_response(resp)
        return self._issue_from_payload(resp.json())

    async def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        since: datetime | None = None,
        per_page: int = 100,
    ) -> list[Issue]:
        """List issues. Paginates automatically up to 1000 issues."""
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since.isoformat()

        out: list[Issue] = []
        url: str | None = f"/repos/{self._config.repo}/issues"
        while url and len(out) < 1000:
            resp = await self._http.get(url, params=params)
            _check_response(resp)
            for payload in resp.json():
                # GH includes PRs in /issues; filter them out.
                if "pull_request" in payload:
                    continue
                out.append(self._issue_from_payload(payload))
            url = self._next_link(resp)
            params = {}  # Link header URLs already have querystrings
        return out

    async def post_comment(self, issue_or_pr_number: int, body: str) -> int:
        """Post a comment on an issue or PR. Returns the new comment id."""
        resp = await self._http.post(
            f"/repos/{self._config.repo}/issues/{issue_or_pr_number}/comments",
            json={"body": body},
        )
        _check_response(resp)
        return int(resp.json()["id"])

    async def add_label(self, issue_or_pr_number: int, *labels: str) -> None:
        """Idempotent: GitHub does not duplicate labels."""
        resp = await self._http.post(
            f"/repos/{self._config.repo}/issues/{issue_or_pr_number}/labels",
            json={"labels": list(labels)},
        )
        _check_response(resp)

    async def remove_label(self, issue_or_pr_number: int, label: str) -> None:
        """Remove a single label. 404 if the label isn't present (we ignore)."""
        resp = await self._http.delete(
            f"/repos/{self._config.repo}/issues/{issue_or_pr_number}/labels/{label}"
        )
        if resp.status_code == 404:
            return
        _check_response(resp)

    # --- PRs ---------------------------------------------------------------

    async def list_open_prs(self, base: str = "develop") -> list[PullRequest]:
        """List open PRs targeting ``base``. Thin wrapper around :meth:`list_prs`."""
        return await self.list_prs(state="open", base=base)

    async def list_prs(
        self,
        *,
        state: str = "open",
        base: str = "develop",
        since: datetime | None = None,
    ) -> list[PullRequest]:
        """List PRs targeting ``base`` in the given state.

        ``state`` is one of ``"open"``, ``"closed"``, or ``"all"``. When
        ``since`` is given and ``state != "open"``, we filter to PRs whose
        ``updated_at`` is at or after ``since`` — useful for poll-back loops
        that only need the recently-closed slice.
        """
        params: dict[str, Any] = {"state": state, "base": base, "per_page": 100}
        # GitHub's /pulls endpoint sorts by created_at by default; sort by
        # updated descending so the freshest items come first (matters for
        # closed-PR polling where we usually want the latest merges).
        if state != "open":
            params["sort"] = "updated"
            params["direction"] = "desc"

        resp = await self._http.get(f"/repos/{self._config.repo}/pulls", params=params)
        _check_response(resp)

        # For each PR, fetch its closing issues via GraphQL. In practice the
        # orchestrator caches this; here we keep it simple.
        prs: list[PullRequest] = []
        for payload in resp.json():
            if since is not None:
                updated = _parse_dt(payload.get("updated_at"))
                if updated is not None and updated < since:
                    # The list is sorted desc by updated when state != open,
                    # so we can stop early.
                    break
            closing = await self._closing_issues_for_pr(payload["number"])
            prs.append(self._pr_from_payload(payload, closing_issues=closing))
        return prs

    async def _closing_issues_for_pr(self, pr_number: int) -> frozenset[int]:
        """Resolve closingIssuesReferences for a PR via GraphQL."""
        owner, name = self._config.repo.split("/", 1)
        query = """
        query($owner: String!, $name: String!, $pr: Int!) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $pr) {
              closingIssuesReferences(first: 20) { nodes { number } }
            }
          }
        }
        """
        resp = await self._http.post(
            self._config.graphql_url,
            json={"query": query, "variables": {"owner": owner, "name": name, "pr": pr_number}},
        )
        _check_response(resp)
        data = resp.json()
        if "errors" in data:
            raise GitHubError(f"GraphQL errors: {data['errors']}")
        nodes = data["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        return frozenset(int(n["number"]) for n in nodes)

    # --- Parsers -----------------------------------------------------------

    @staticmethod
    def _issue_from_payload(payload: dict[str, Any]) -> Issue:
        return Issue(
            number=int(payload["number"]),
            title=str(payload["title"]),
            body=str(payload.get("body") or ""),
            state=str(payload["state"]),
            labels=frozenset(label["name"] for label in payload.get("labels", [])),
            created_at=_parse_dt(payload["created_at"]) or datetime.utcnow(),
            updated_at=_parse_dt(payload["updated_at"]) or datetime.utcnow(),
            closed_at=_parse_dt(payload.get("closed_at")),
        )

    @staticmethod
    def _pr_from_payload(payload: dict[str, Any], closing_issues: frozenset[int]) -> PullRequest:
        # CRITICAL: the GitHub LIST endpoint (/repos/{}/pulls) does NOT
        # include the `merged` field — only the single-PR endpoint does.
        # If we trust `payload.get("merged", False)` we silently treat
        # every closed PR returned from list_prs as "not merged", which
        # breaks the pollback merged-batch reconciliation.
        # Fallback: if `merged` is absent, derive from `merged_at`.
        merged_at = _parse_dt(payload.get("merged_at"))
        merged = bool(payload["merged"]) if "merged" in payload else merged_at is not None
        return PullRequest(
            number=int(payload["number"]),
            title=str(payload["title"]),
            body=str(payload.get("body") or ""),
            head_ref=str(payload["head"]["ref"]),
            base_ref=str(payload["base"]["ref"]),
            state=str(payload["state"]),
            merged=merged,
            labels=frozenset(label["name"] for label in payload.get("labels", [])),
            closing_issues=closing_issues,
            created_at=_parse_dt(payload["created_at"]) or datetime.utcnow(),
            updated_at=_parse_dt(payload["updated_at"]) or datetime.utcnow(),
            merged_at=merged_at,
        )

    @staticmethod
    def _next_link(resp: httpx.Response) -> str | None:
        """Parse the ``Link`` header to find the next page URL, if any."""
        link = resp.headers.get("link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                start = part.find("<") + 1
                end = part.find(">")
                if 0 < start < end:
                    return part[start:end]
        return None
