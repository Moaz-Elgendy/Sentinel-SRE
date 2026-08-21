"""
Structured JSON logging.

Loki/Promtail can scrape stdout and parse these as JSON lines, giving us
queryable fields (level, request_id, path, etc.) without extra plumbing.
Never log secrets, passwords, or full request bodies here.
"""
import logging
import sys

from pythonjsonlogger import jsonlogger


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
        
        try:
            from app.middleware.request_id import get_request_id
            req_id = get_request_id()
            if req_id:
                record.request_id = req_id
        except Exception:
            pass
            
        return record

    logging.setLogRecordFactory(record_factory)
