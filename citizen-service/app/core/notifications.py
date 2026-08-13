"""
Client for calling the Notification Service.

Deliberately fire-and-forget: a notification failure must never fail the
citizen-facing request that triggered it (e.g. submitting a service
request should still succeed even if notification-service is down).
Failures are logged so Sentinel can pick them up as a symptom of a
downstream outage without it cascading into citizen-service errors.
"""
import logging
import uuid

import httpx

from app.core.config import settings
from app.core.metrics import notification_dispatches_total
from app.middleware.request_id import get_request_id

logger = logging.getLogger(__name__)


class NotificationClient:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(
        self,
        *,
        citizen_id: uuid.UUID,
        event_type: str,
        recipient: str,
        message: str,
        request_id: uuid.UUID | None = None,
        channel: str = "email",
    ) -> None:
        try:
            headers = {}
            try:
                req_id = get_request_id()
                if req_id:
                    headers["X-Request-ID"] = req_id
            except Exception:
                pass

            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/api/notifications",
                    headers=headers,
                    json={
                        "citizen_id": str(citizen_id),
                        "request_id": str(request_id) if request_id else None,
                        "event_type": event_type,
                        "channel": channel,
                        "recipient": recipient,
                        "message": message,
                    },
                )
                resp.raise_for_status()
                notification_dispatches_total.labels(result="success").inc()
        except httpx.HTTPError as exc:
            notification_dispatches_total.labels(result="failure").inc()
            logger.warning(
                "notification_dispatch_failed",
                extra={"event_type": event_type, "error": str(exc)},
            )


def get_notification_client() -> NotificationClient:
    return NotificationClient(base_url=settings.notification_service_url)
