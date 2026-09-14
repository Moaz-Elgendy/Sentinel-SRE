"""
Structured JSON logging.

Copied from citizen-service/app/core/logging_config.py on purpose: Loki
already has dashboards and alert rules keyed off this exact field shape
(`timestamp`, `level`, `name`, `message`, `service`), so Sentinel must emit
the same shape or it becomes the one service you cannot correlate — which
would be a bad joke for the service whose whole job is correlation.

Difference from the app services: there is no request-id ContextVar here.
Sentinel's unit of work is an *incident*, not an HTTP request, so we attach
`incident_id` instead, and only when we are inside a lifecycle run. The key
is ABSENT (not null) outside an incident, matching how the app services treat
`request_id` — Loki's JSON parser is happier with absent keys than nulls.

Never log secrets: CHAOS_ADMIN_TOKEN, OPENAI_API_KEY, GITHUB_TOKEN and the
Slack webhook URL must never reach a log line.
"""
import contextvars
import logging
import sys

from pythonjsonlogger import jsonlogger

# Set by the orchestrator for the duration of one incident lifecycle run.
# ContextVar (not a global) so concurrent incidents processed in separate
# asyncio tasks do not smear each other's ids across log lines.
_incident_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sentinel_incident_id", default=None
)


def set_incident_id(incident_id: str | None) -> None:
    _incident_id.set(incident_id)


def get_incident_id() -> str | None:
    return _incident_id.get()


def configure_logging(service_name: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    # Tag every record with the originating service for easy Loki filtering.
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.service = service_name

        incident_id = _incident_id.get()
        if incident_id:
            record.incident_id = incident_id

        return record

    logging.setLogRecordFactory(record_factory)

    # The kubernetes client's urllib3 layer logs every request at DEBUG and
    # the openai client is similarly chatty. We keep root at INFO, but these
    # two get pushed to WARNING because a single lifecycle run can otherwise
    # produce hundreds of lines of transport noise per incident.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
