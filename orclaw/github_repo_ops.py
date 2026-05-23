"""GitHub data-API helpers for editing arbitrary repos.

The existing :mod:`orclaw.github_client` is geared toward the
specific issue/PR workflow of ${TARGET_REPO}. This module adds the lower-level
git data API (blobs, trees, commits, refs) — what we need to compose
multi-file commits into orclaw itself for the auto-deploy
workflow.

Each function is a thin async wrapper around one or two GitHub REST
calls. They take a ``repo`` arg explicitly so callers can target any
repo they have access to (not just ${TARGET_REPO}).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from orclaw.exceptions import GitHubAuthError, GitHubError, RateLimitedError
from orclaw.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

log = get_logger(__name__)


_BASE_URL = "https://api.github.com"
_USER_AGENT = "orclaw/0.1"


@dataclass(frozen=True, slots=True)
class CommitResult:
    """What :func:`commit_multi_file_change` produces."""

    branch: str
    commit_sha: str
    pr_number: int
    pr_url: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise GitHubAuthError("GitHub returned 401 — token expired or wrong scope.")
    if resp.status_code == 403:
        if resp.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimitedError(
                f"Rate limit hit. Reset at {resp.headers.get('x-ratelimit-reset')}"
            )
        raise GitHubAuthError(f"GitHub 403: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub {resp.status_code}: {resp.text[:500]}")


async def _get_ref_sha(client: httpx.AsyncClient, token: str, repo: str, branch: str) -> str:
    resp = await client.get(
        f"{_BASE_URL}/repos/{repo}/git/refs/heads/{branch}",
        headers=_headers(token),
    )
    _check(resp)
    return str(resp.json()["object"]["sha"])


async def _get_commit_tree(
    client: httpx.AsyncClient, token: str, repo: str, commit_sha: str
) -> str:
    resp = await client.get(
        f"{_BASE_URL}/repos/{repo}/git/commits/{commit_sha}",
        headers=_headers(token),
    )
    _check(resp)
    return str(resp.json()["tree"]["sha"])


async def _create_blob(client: httpx.AsyncClient, token: str, repo: str, content: str) -> str:
    """Create a blob (base64-encoded so binaries + UTF-8 both work)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    resp = await client.post(
        f"{_BASE_URL}/repos/{repo}/git/blobs",
        headers=_headers(token),
        json={"content": encoded, "encoding": "base64"},
    )
    _check(resp)
    return str(resp.json()["sha"])


async def _create_tree(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    base_tree_sha: str,
    files: Mapping[str, str],
) -> str:
    """Create a tree extending ``base_tree_sha`` with the given file paths
    (mapping path → blob SHA)."""
    tree_items = [
        {"path": path, "mode": "100644", "type": "blob", "sha": sha} for path, sha in files.items()
    ]
    resp = await client.post(
        f"{_BASE_URL}/repos/{repo}/git/trees",
        headers=_headers(token),
        json={"base_tree": base_tree_sha, "tree": tree_items},
    )
    _check(resp)
    return str(resp.json()["sha"])


async def _create_commit(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    message: str,
    tree_sha: str,
    parent_sha: str,
) -> str:
    resp = await client.post(
        f"{_BASE_URL}/repos/{repo}/git/commits",
        headers=_headers(token),
        json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
    )
    _check(resp)
    return str(resp.json()["sha"])


async def _create_branch(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    branch: str,
    commit_sha: str,
) -> None:
    resp = await client.post(
        f"{_BASE_URL}/repos/{repo}/git/refs",
        headers=_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
    )
    _check(resp)


async def _open_pull_request(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict[str, object]:
    resp = await client.post(
        f"{_BASE_URL}/repos/{repo}/pulls",
        headers=_headers(token),
        json={"title": title, "body": body, "head": head, "base": base},
    )
    _check(resp)
    return resp.json()  # type: ignore[no-any-return]


# --- Public entrypoints ----------------------------------------------------


async def commit_multi_file_change(
    *,
    token: str,
    repo: str,
    branch: str,
    files: Mapping[str, str],
    commit_message: str,
    pr_title: str,
    pr_body: str = "",
    base_branch: str = "main",
) -> CommitResult:
    """Compose a multi-file commit + push to a new branch + open a PR.

    The whole operation is performed via the GitHub git data API — no
    local git checkout required. Atomic from the caller's perspective:
    either everything happens (returns CommitResult) or an exception is
    raised before any visible change.

    ``files`` keys are paths from repo root (e.g. ``orclaw/cli.py``).
    Values are the full new file contents (UTF-8 text).

    The branch ``branch`` must not exist yet. Pick a descriptive name like
    ``feat/raise-concurrency-cap`` or ``fix/typo-in-readme``.
    """
    if not files:
        raise ValueError("at least one file required in `files`")
    if not branch or "/" not in branch:
        raise ValueError("branch must be non-empty and prefixed (e.g. 'feat/xxx', 'fix/yyy')")

    async with httpx.AsyncClient(timeout=30.0) as client:
        base_sha = await _get_ref_sha(client, token, repo, base_branch)
        base_tree = await _get_commit_tree(client, token, repo, base_sha)

        # Sequential to avoid hammering the API; usually 1-3 files.
        blob_shas: dict[str, str] = {}
        for path, content in files.items():
            blob_shas[path] = await _create_blob(client, token, repo, content)

        tree_sha = await _create_tree(client, token, repo, base_tree, blob_shas)
        commit_sha = await _create_commit(client, token, repo, commit_message, tree_sha, base_sha)
        await _create_branch(client, token, repo, branch, commit_sha)

        pr = await _open_pull_request(
            client,
            token,
            repo,
            title=pr_title,
            body=pr_body,
            head=branch,
            base=base_branch,
        )

    pr_number_raw = pr["number"]
    assert isinstance(pr_number_raw, int), "GitHub PR number should be int"
    return CommitResult(
        branch=branch,
        commit_sha=commit_sha,
        pr_number=pr_number_raw,
        pr_url=str(pr["html_url"]),
    )


async def get_pull_request(*, token: str, repo: str, pr_number: int) -> dict[str, object]:
    """Fetch a single PR's full payload + diff stats."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE_URL}/repos/{repo}/pulls/{pr_number}",
            headers=_headers(token),
        )
        _check(resp)
        return resp.json()  # type: ignore[no-any-return]


async def get_pull_request_files(
    *, token: str, repo: str, pr_number: int
) -> list[dict[str, object]]:
    """Per-file diff info (filename, additions, deletions, status, patch)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE_URL}/repos/{repo}/pulls/{pr_number}/files",
            headers=_headers(token),
            params={"per_page": 100},
        )
        _check(resp)
        return list(resp.json())


async def merge_pull_request(
    *,
    token: str,
    repo: str,
    pr_number: int,
    merge_method: str = "merge",
    commit_message: str | None = None,
) -> dict[str, object]:
    """Merge a PR. ``merge_method`` is one of ``merge``, ``squash``, ``rebase``."""
    payload: dict[str, object] = {"merge_method": merge_method}
    if commit_message is not None:
        payload["commit_message"] = commit_message

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.put(
            f"{_BASE_URL}/repos/{repo}/pulls/{pr_number}/merge",
            headers=_headers(token),
            json=payload,
        )
        _check(resp)
        return resp.json()  # type: ignore[no-any-return]
