"""
Quick standalone test of Sentinel's SlackClient, isolated from the rest of
the app (no k8s, no Prometheus, no cluster needed).

Usage:
    export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
    python test_slack.py
"""
import asyncio
import os
import sys

# Make Sentinel's app/ package importable when run from sentinel-ai/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "sentinel-ai"))

from app.clients.slack_client import SlackClient  # noqa: E402


async def main() -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set — export it first.")
        return

    client = SlackClient(webhook_url)
    print(f"enabled: {client.enabled}")

    result = await client.post(
        ":rotating_light: *Sentinel AI — test notification*\n"
        "This is a manual test of the incoming webhook, not a real incident.\n"
        "If you see this in Slack, the webhook is wired correctly."
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
