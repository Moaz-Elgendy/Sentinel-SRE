"""
GitHub issue creation (optional, no-op without a token).

Sentinel writes a post-incident issue containing the timeline, RCA, impact
and preventive actions. That is ALL it does with GitHub.

**Hard constraint, enforced by the absence of code rather than by a check:**
Sentinel never modifies application source, never opens a pull request,
never pushes a branch, never merges anything. The only API call in this file
is `POST /repos/{owner}/{repo}/issues`. Proposed code changes are *text
inside the issue body* for a human to read and act on. An autonomous agent
that can restart a Deployment is a well-bounded risk; one that can rewrite
the code and merge it is not, and the boundary is drawn here deliberately.

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
