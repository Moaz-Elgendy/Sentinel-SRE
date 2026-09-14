"""
GitHubClient.get_commit() — read-only commit lookup for deployment
correlation.

No real network calls: httpx.AsyncClient is monkeypatched with a minimal
stand-in that returns a canned response, matching the level this codebase
already tests its other HTTP clients at (i.e. not at all, previously — this
file adds that coverage for the one GitHub method that matters for RCA).
"""
from __future__ import annotations

import pytest

from app.clients.github_client import GitHubClient, _looks_like_sha


# ---------------------------------------------------------------------------
# _looks_like_sha — the guard that stops a CI smoke-test tag or "latest"
# from ever reaching the GitHub API as if it were a commit.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345", True),  # full 40-char SHA
        ("9f3c1a8", True),  # short 7-char SHA
        ("latest", False),
        ("manual-test-18234567890", False),
        ("", False),
        ("GHIJKL1", False),  # not hex
        ("9f3c1a", False),  # 6 chars, one short of the floor
    ],
)
def test_looks_like_sha(value, expected):
    assert _looks_like_sha(value) is expected


# ---------------------------------------------------------------------------
# get_commit() — the HTTP path, mocked
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Replaces httpx.AsyncClient for the duration of one test.

    get_commit() constructs a NEW `async with httpx.AsyncClient(...)` for
    each of its two calls (commit, then pulls) rather than reusing one
    client — so this holds a reference to a single shared list and pops
    from it, instead of copying, or the second construction would just see
    the first response again.
    """

    def __init__(self, responses):
        self._responses = responses  # shared reference, not a copy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return self._responses.pop(0)


def _patch_httpx(monkeypatch, responses):
    import app.clients.github_client as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeAsyncClient(responses))


@pytest.mark.asyncio
async def test_get_commit_disabled_without_token():
    client = GitHubClient(token="", repository="")
    assert await client.get_commit("9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345") is None


@pytest.mark.asyncio
async def test_get_commit_skips_non_sha_tags_without_any_http_call(monkeypatch):
    client = GitHubClient(token="t", repository="org/repo")

    def _fail_if_called(*a, **kw):
        raise AssertionError("httpx.AsyncClient must not be constructed for a non-SHA tag")

    monkeypatch.setattr("app.clients.github_client.httpx.AsyncClient", _fail_if_called)
    assert await client.get_commit("latest") is None
    assert await client.get_commit("manual-test-18234567890") is None


@pytest.mark.asyncio
async def test_get_commit_success_parses_message_files_and_pr(monkeypatch):
    sha = "9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345"
    commit_response = _FakeResponse(
        200,
        {
            "sha": sha,
            "html_url": f"https://github.com/org/repo/commit/{sha}",
            "commit": {
                "message": "widen db connection pool timeout\n\nlonger body here",
                "author": {"name": "moaz", "date": "2026-08-20T10:00:00Z"},
            },
            "files": [{"filename": "app/db.py"}, {"filename": "app/config.py"}],
        },
    )
    pr_response = _FakeResponse(
        200, [{"number": 42, "title": "fix pool timeout", "html_url": "https://x/pr/42"}]
    )
    _patch_httpx(monkeypatch, [commit_response, pr_response])

    client = GitHubClient(token="t", repository="org/repo")
    result = await client.get_commit(sha)

    assert result is not None
    assert result["sha"] == sha[:12]
    # Only the first line of the message, per the module's docstring intent.
    assert result["message"] == "widen db connection pool timeout"
    assert result["author"] == "moaz"
    assert result["changed_files"] == ["app/db.py", "app/config.py"]
    assert result["changed_file_count"] == 2
    assert result["pull_request"] == {
        "number": 42,
        "title": "fix pool timeout",
        "url": "https://x/pr/42",
    }


@pytest.mark.asyncio
async def test_get_commit_returns_none_on_404_not_an_exception(monkeypatch):
    _patch_httpx(monkeypatch, [_FakeResponse(404)])
    client = GitHubClient(token="t", repository="org/repo")
    result = await client.get_commit("9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345")
    assert result is None


@pytest.mark.asyncio
async def test_get_commit_missing_pr_still_returns_commit_data(monkeypatch):
    """No associated PR is a normal case (direct push to main), not a failure."""
    sha = "9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345"
    commit_response = _FakeResponse(
        200,
        {
            "sha": sha,
            "commit": {"message": "hotfix", "author": {"name": "moaz"}},
            "files": [],
        },
    )
    pr_response = _FakeResponse(200, [])
    _patch_httpx(monkeypatch, [commit_response, pr_response])

    client = GitHubClient(token="t", repository="org/repo")
    result = await client.get_commit(sha)

    assert result is not None
    assert result["pull_request"] is None
    assert result["changed_files"] == []
