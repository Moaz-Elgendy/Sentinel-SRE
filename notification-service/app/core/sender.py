"""
Simulated notification delivery.

No real email/SMS provider is integrated in this demo — sending a
notification means validating it, logging it, and persisting the outcome.
This keeps the service fully runnable offline while still producing
realistic "delivery" telemetry for Sentinel to observe.

When CHAOS_MODE is enabled (Phase 10), a configurable fraction of sends
are deliberately marked as failed so the platform has a controlled way to
generate a notification-delivery incident.
"""
import logging
import random
import time

from app.chaos.state import controller
from app.core.config import settings
from app.core.metrics import notification_delivery_duration_seconds
from app.models.notification import NotificationStatus

logger = logging.getLogger(__name__)


def deliver(*, channel: str, recipient: str, message: str) -> tuple[NotificationStatus, str | None]:
    """Simulate handing a message off to an email/SMS provider.

    Returns (status, error_detail). error_detail is None on success.
    """
    start_time = time.time()
    
    failure_rate = controller.get().notification_failure_rate
    if settings.chaos_mode and random.random() < failure_rate:
        controller.record("notification_delivery")
        error_detail = f"simulated {channel} provider timeout"
        logger.warning(
            "notification_delivery_failed",
            extra={"channel": channel, "recipient": recipient, "reason": error_detail},
        )
        notification_delivery_duration_seconds.labels(channel=channel).observe(time.time() - start_time)
        return NotificationStatus.failed, error_detail

    logger.info("notification_delivered", extra={"channel": channel, "recipient": recipient})
    notification_delivery_duration_seconds.labels(channel=channel).observe(time.time() - start_time)
    return NotificationStatus.sent, None
