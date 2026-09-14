"""
Slack incoming-webhook notifier (optional, no-op without a URL).

Deliberately minimal: one POST to the configured webhook with a text
payload. No Slack app, no OAuth, no interactive buttons.

No buttons specifically because Sentinel is *autonomous* — there is no
"approve this rollback?" interaction to build. Slack here is a notification
sink, not a control plane. If someone later wants a human approval gate, the
right place is DRY_RUN=true plus the /api/incidents API, not an
internet-facing callback that can trigger a cluster write.

The webhook URL is a secret (it is a bearer capability for posting to that
channel). It is never logged, and never included in an incident record.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SlackClient:
    def __init__(self, webhook_url: str, timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip())

    async def post(self, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Post a message. Never raises; returns a small status dict."""
        if not self.enabled:
            return {
                "sent": False,
                "skipped": True,
                "detail": "SLACK_WEBHOOK_URL not configured; notification not sent",
            }

        # Slack truncates around 40k characters per message and gets unhappy
        # well before that with mrkdwn. Incident notifications are summaries;
        # the full record lives in SQLite and (optionally) a GitHub issue.
        payload: dict[str, Any] = {"text": text[:3500]}
        if blocks:
            payload["blocks"] = blocks

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("slack_post_failed", extra={"error_detail": str(exc)[:200]})
            return {"sent": False, "skipped": False, "detail": str(exc)[:200]}

        if resp.status_code != 200:
            logger.warning("slack_post_rejected", extra={"status_code": resp.status_code})
            return {
                "sent": False,
                "skipped": False,
                "detail": f"Slack returned HTTP {resp.status_code}",
            }

        logger.info("slack_notification_sent")
        return {"sent": True, "skipped": False, "detail": "ok"}
