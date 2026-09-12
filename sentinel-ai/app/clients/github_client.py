"""
GitHub integration: issue creation, and read-only commit lookup for
deployment/regression correlation. That is ALL Sentinel does with GitHub.

**Hard constraint, enforced by the absence of code rather than by a check:**
Sentinel never modifies application source, never opens a pull request,
never pushes a branch, never merges anything. The only WRITE call in this
file is `POST /repos/{owner}/{repo}/issues`. `get_commit()` is GET-only —
it reads a commit's metadata and file list, it does not touch source, and
the token this client is given only ever needs `contents: read` +
`issues: write` (fine-grained) or `repo` scope (classic), never anything
broader. Proposed code changes are *text inside the issue body* for a human
to read and act on. An autonomous agent that can restart a Deployment is a
well-bounded risk; one that can rewrite the code and merge it is not, and
the boundary is drawn here deliberately.

If you are tempted to add a `create_pull_request` method: don't. Add it to
the issue body as a suggested diff instead.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, repository: str, timeout: float = 10.0) -> None:
        # repository is "owner/repo".
        self.token = token
        self.repository = repository
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip() and "/" in self.repository)

    async def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        """Create an issue. Returns a dict describing what happened.

        Never raises. Documentation delivery failing must not turn a
        successful remediation into a failed incident — the incident record in
        SQLite is the authoritative copy; GitHub and Slack are conveniences.
        """
        if not self.enabled:
            return {
                "created": False,
                "skipped": True,
                "detail": "GITHUB_TOKEN/GITHUB_REPOSITORY not configured; issue not created",
            }

        url = f"{GITHUB_API}/repos/{self.repository}/issues"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # GitHub rejects bodies over 65536 characters. An incident with a long
        # log sample can get close, so truncate with an explicit marker rather
        # than letting the API 422.
        if len(body) > 60_000:
            body = body[:60_000] + "\n\n_(truncated by Sentinel: body exceeded 60k chars)_"

        payload: dict[str, Any] = {"title": title[:250], "body": body}
        if labels:
            payload["labels"] = labels

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("github_issue_failed", extra={"error_detail": str(exc)[:200]})
            return {"created": False, "skipped": False, "detail": str(exc)[:200]}

        if resp.status_code not in (200, 201):
            # Deliberately not logging the response body verbatim at INFO —
            # GitHub error bodies can echo request content.
            logger.warning(
                "github_issue_rejected", extra={"status_code": resp.status_code}
            )
            return {
                "created": False,
                "skipped": False,
                "detail": f"GitHub returned HTTP {resp.status_code}",
            }

        try:
            data = resp.json()
        except ValueError:
            data = {}
        logger.info("github_issue_created", extra={"issue_number": data.get("number")})
        return {
            "created": True,
            "skipped": False,
            "number": data.get("number"),
            "url": data.get("html_url"),
            "detail": "issue created",
        }

    async def get_commit(self, sha: str) -> dict[str, Any] | None:
        """GET /repos/{owner}/{repo}/commits/{sha} — read-only, never raises.

        Returns None (not an exception) on any failure: unreachable API,
        unknown/short/malformed SHA, rate limit, or GITHUB_TOKEN/REPOSITORY
        not configured. This is deployment-correlation evidence, not a
        remediation input, so a failure here must never abort the incident
        lifecycle — RCA proceeds without the commit context and says so.

        `sha` is taken verbatim from a container image tag pulled out of
        Kubernetes ReplicaSet history (see investigation.py) — never from
        LLM output, never from user-controlled alert text — so there is no
        injection surface in how this method gets called.
        """
        if not self.enabled or not sha:
            return None

        # A short/garbled tag (e.g. "latest", "manual-test-123" from the CI
        # smoke-test job) is not a commit SHA. GitHub's API would 404 or
        # 422 on it anyway, but checking first avoids a pointless round trip
        # and makes the "no commit evidence" reason explicit in logs.
        if not _looks_like_sha(sha):
            logger.info(
                "github_commit_skipped_non_sha_tag",
                extra={"image_tag": sha[:40]},
            )
            return None

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        commit_url = f"{GITHUB_API}/repos/{self.repository}/commits/{sha}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(commit_url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("github_commit_unreachable", extra={"error_detail": str(exc)[:200]})
            return None

        if resp.status_code != 200:
            # 404 here is unambiguous (unlike the chaos client's 404) — it
            # just means this SHA is not in the repository, e.g. the image
            # was built from a branch/fork this token cannot see, or the
            # tag was never a real commit.
            logger.info(
                "github_commit_not_found",
                extra={"status_code": resp.status_code, "sha": sha[:12]},
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        commit_info = data.get("commit") or {}
        author_info = commit_info.get("author") or {}
        files = data.get("files") or []

        result: dict[str, Any] = {
            "sha": data.get("sha", sha)[:12],
            "message": (commit_info.get("message") or "").split("\n")[0][:300],
            "author": author_info.get("name") or (data.get("author") or {}).get("login"),
            "authored_at": author_info.get("date"),
            "url": data.get("html_url"),
            # Filenames only, capped — this is correlation context for the
            # RCA narrative, not a diff viewer, and commit payloads can be
            # large on big commits.
            "changed_files": [f.get("filename") for f in files[:25] if f.get("filename")],
            "changed_file_count": len(files),
            "pull_request": None,
        }

        # Best-effort only. Not every commit has an associated PR (direct
        # pushes to main, squash-merges attributed differently, etc.), and
        # this must never fail the whole lookup.
        pr_url = f"{commit_url}/pulls"
        pr_headers = dict(headers)
        pr_headers["Accept"] = "application/vnd.github.groot-preview+json"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                pr_resp = await client.get(pr_url, headers=pr_headers)
            if pr_resp.status_code == 200:
                prs = pr_resp.json()
                if prs:
                    pr = prs[0]
                    result["pull_request"] = {
                        "number": pr.get("number"),
                        "title": pr.get("title"),
                        "url": pr.get("html_url"),
                    }
        except httpx.HTTPError:
            pass  # PR linkage is a bonus field; commit data above still stands.

        logger.info(
            "github_commit_correlated",
            extra={"sha": result["sha"], "changed_file_count": result["changed_file_count"]},
        )
        return result


def _looks_like_sha(value: str) -> bool:
    """A git SHA (full or short) is hex, nothing else. Rejects tags like
    'latest', 'manual-test-18234567', or an empty/None tag."""
    v = value.strip().lower()
    return 7 <= len(v) <= 40 and all(c in "0123456789abcdef" for c in v)
