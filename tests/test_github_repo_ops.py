"""Tests for github_repo_ops.

We mock the HTTP layer with ``httpx.MockTransport``. Real API calls
would require a live PAT — those happen in integration runs, not here.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

import orclaw.github_repo_ops as gro
from orclaw.exceptions import GitHubAuthError, GitHubError, RateLimitedError
from orclaw.github_repo_ops import (
    CommitResult,
    commit_multi_file_change,
    get_pull_request,
    get_pull_request_files,
    merge_pull_request,
)


@pytest.fixture()
def mock_http(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], list[httpx.Request]]:
    """Patch httpx.AsyncClient with a MockTransport. Returns a setter
    that takes a handler and yields the list of captured requests.
    """

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        captured: list[httpx.Request] = []

        def tracking_handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return handler(req)

        transport = httpx.MockTransport(tracking_handler)
        real_async_client = httpx.AsyncClient

        def factory_fn(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("timeout", None)
            return real_async_client(transport=transport)

        monkeypatch.setattr(gro.httpx, "AsyncClient", factory_fn)
        return captured

    return factory


class TestCommitMultiFileChange:
    @pytest.mark.asyncio
    async def test_happy_path_makes_six_api_calls_returns_result(self, mock_http) -> None:
        # Sequence:
        #  1. GET /git/refs/heads/main → {object.sha}
        #  2. GET /git/commits/{sha} → {tree.sha}
        #  3. POST /git/blobs (one per file)
        #  4. POST /git/trees
        #  5. POST /git/commits
        #  6. POST /git/refs
        #  7. POST /pulls
        responses = [
            httpx.Response(200, json={"object": {"sha": "base123"}}),
            httpx.Response(200, json={"tree": {"sha": "tree456"}}),
            httpx.Response(201, json={"sha": "blobA"}),
            httpx.Response(201, json={"sha": "blobB"}),
            httpx.Response(201, json={"sha": "newtree"}),
            httpx.Response(201, json={"sha": "commitXYZ"}),
            httpx.Response(201, json={}),  # ref creation
            httpx.Response(
                201,
                json={
                    "number": 42,
                    "html_url": "https://github.com/owner/repo/pull/42",
                },
            ),
        ]

        idx = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal idx
            resp = responses[idx]
            idx += 1
            return resp

        captured = mock_http(handler)

        result = await commit_multi_file_change(
            token="ghp_x",
            repo="me/repo",
            branch="fix/typo",
            files={"a.py": "print('a')\n", "b.py": "print('b')\n"},
            commit_message="fix: typo",
            pr_title="fix: typo",
            pr_body="why this matters",
        )

        assert isinstance(result, CommitResult)
        assert result.commit_sha == "commitXYZ"
        assert result.pr_number == 42
        assert result.pr_url.endswith("/pull/42")
        assert result.branch == "fix/typo"

        # Quick check on the blob POST payloads — content must be
        # base64-encoded.
        blob_reqs = [r for r in captured if r.url.path.endswith("/git/blobs")]
        assert len(blob_reqs) == 2
        for r in blob_reqs:
            body = json.loads(r.content)
            assert body["encoding"] == "base64"
            decoded = base64.b64decode(body["content"]).decode()
            assert decoded.startswith("print(")

    @pytest.mark.asyncio
    async def test_empty_files_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one file"):
            await commit_multi_file_change(
                token="x",
                repo="r/r",
                branch="fix/x",
                files={},
                commit_message="x",
                pr_title="x",
            )

    @pytest.mark.asyncio
    async def test_branch_must_be_prefixed(self) -> None:
        with pytest.raises(ValueError, match="prefixed"):
            await commit_multi_file_change(
                token="x",
                repo="r/r",
                branch="just-a-name",  # no slash
                files={"x.py": "x"},
                commit_message="x",
                pr_title="x",
            )

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, mock_http) -> None:
        mock_http(lambda req: httpx.Response(401, text="Bad credentials"))
        with pytest.raises(GitHubAuthError):
            await commit_multi_file_change(
                token="bad",
                repo="r/r",
                branch="fix/x",
                files={"x.py": "x"},
                commit_message="x",
                pr_title="x",
            )

    @pytest.mark.asyncio
    async def test_rate_limit_raises_specific(self, mock_http) -> None:
        mock_http(
            lambda req: httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1234567"},
                text="rate limit",
            )
        )
        with pytest.raises(RateLimitedError):
            await commit_multi_file_change(
                token="x",
                repo="r/r",
                branch="fix/x",
                files={"x.py": "x"},
                commit_message="x",
                pr_title="x",
            )


class TestGetPullRequest:
    @pytest.mark.asyncio
    async def test_returns_full_payload(self, mock_http) -> None:
        mock_http(
            lambda req: httpx.Response(200, json={"number": 7, "title": "x", "state": "open"})
        )
        pr = await get_pull_request(token="x", repo="r/r", pr_number=7)
        assert pr["number"] == 7
        assert pr["state"] == "open"

    @pytest.mark.asyncio
    async def test_500_raises_generic(self, mock_http) -> None:
        mock_http(lambda req: httpx.Response(500, text="boom"))
        with pytest.raises(GitHubError):
            await get_pull_request(token="x", repo="r/r", pr_number=7)


class TestGetPullRequestFiles:
    @pytest.mark.asyncio
    async def test_returns_file_array(self, mock_http) -> None:
        mock_http(
            lambda req: httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
                        "additions": 5,
                        "deletions": 2,
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n-old\n+new\n",
                    }
                ],
            )
        )
        files = await get_pull_request_files(token="x", repo="r/r", pr_number=7)
        assert len(files) == 1
        assert files[0]["filename"] == "a.py"


class TestMergePullRequest:
    @pytest.mark.asyncio
    async def test_default_merge_method(self, mock_http) -> None:
        captured = mock_http(
            lambda req: httpx.Response(200, json={"merged": True, "sha": "merge_sha"})
        )
        result = await merge_pull_request(token="x", repo="r/r", pr_number=7)
        assert result["merged"] is True
        body = json.loads(captured[0].content)
        assert body["merge_method"] == "merge"

    @pytest.mark.asyncio
    async def test_squash_method_propagates(self, mock_http) -> None:
        captured = mock_http(lambda req: httpx.Response(200, json={"merged": True}))
        await merge_pull_request(token="x", repo="r/r", pr_number=7, merge_method="squash")
        body = json.loads(captured[0].content)
        assert body["merge_method"] == "squash"

    @pytest.mark.asyncio
    async def test_405_when_not_mergeable(self, mock_http) -> None:
        # GitHub returns 405 when the PR isn't in a mergeable state.
        mock_http(lambda req: httpx.Response(405, text="not mergeable"))
        with pytest.raises(GitHubError):
            await merge_pull_request(token="x", repo="r/r", pr_number=7)
