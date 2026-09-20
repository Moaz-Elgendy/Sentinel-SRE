"""Tests for app/core/log_capture.py — the Sentinel Logs GUI page's backend:
persistent redacted log lines, and filtered querying of them."""
from __future__ import annotations

import json
import logging

from app.core.log_capture import (
    PersistentLogHandler,
    RedactionFilter,
    _redact_text,
    query_logs,
)


def _make_handler(tmp_path, bus=None):
    handler = PersistentLogHandler(
        log_dir=str(tmp_path), max_bytes=1_000_000, backup_count=3
    )
    handler.addFilter(RedactionFilter())
    if bus is not None:
        handler.set_bus(bus)
    return handler


def _emit(handler, logger_name, level, msg, **extra):
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    # log_capture.query_logs filters on `timestamp_epoch`, which in
    # production is stamped by the record factory in logging_config.py
    # (configure_logging), not by logging.LogRecord itself.
    import time

    record.timestamp_epoch = time.time()
    handler.handle(record)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_redact_text_scrubs_a_bearer_token():
    text = "calling kubernetes with Authorization: Bearer abc.def-123_ZZ"
    assert "abc.def-123_ZZ" not in _redact_text(text)


def test_redact_text_scrubs_an_openai_style_key():
    text = "using key sk-ABCDEFGHIJKLMNOPQRSTUVWX for the call"
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in _redact_text(text)


def test_redaction_filter_scrubs_message_before_it_reaches_the_file(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(
        handler,
        "app.clients.github_client",
        logging.INFO,
        "authenticated with token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    )

    written = (tmp_path / "sentinel.log").read_text()
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in written
    assert "[REDACTED]" in written


def test_redaction_filter_scrubs_a_secret_shaped_extra_field(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(
        handler,
        "app.core.config",
        logging.INFO,
        "loaded settings",
        chaos_admin_token="super-secret-value",
    )

    entry = json.loads((tmp_path / "sentinel.log").read_text().strip())
    assert entry["chaos_admin_token"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Persistence + live bus fan-out
# ---------------------------------------------------------------------------
def test_emit_writes_one_json_line_per_record(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.lifecycle.rca", logging.INFO, "RCA complete: confidence 0.92")
    _emit(handler, "app.lifecycle.remediation", logging.INFO, "Executing rollback")

    lines = (tmp_path / "sentinel.log").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["message"] == "RCA complete: confidence 0.92"
    assert first["name"] == "app.lifecycle.rca"


def test_emit_also_publishes_to_the_bus_when_one_is_attached(tmp_path):
    published = []

    class _FakeBus:
        def publish(self, event):
            published.append(event)

    handler = _make_handler(tmp_path, bus=_FakeBus())
    _emit(handler, "app.lifecycle.validation", logging.INFO, "Recovery confirmed")

    assert len(published) == 1
    assert published[0]["type"] == "log"
    assert published[0]["message"] == "Recovery confirmed"


def test_emit_without_a_bus_never_raises(tmp_path):
    handler = _make_handler(tmp_path)  # no set_bus() call at all
    _emit(handler, "app.main", logging.INFO, "startup complete")  # must not raise


def test_a_directory_that_cannot_be_created_disables_capture_without_raising():
    # A file (not a directory) at this path makes `Path(...).mkdir()` raise
    # NotADirectoryError/FileExistsError underneath it — exercising the same
    # "log capture must never be able to crash startup" contract as an
    # unwritable /var/lib/sentinel, without needing real permission tricks.
    import tempfile

    with tempfile.NamedTemporaryFile() as f:
        blocked_dir = f"{f.name}/logs"
        handler = _make_handler(blocked_dir)
        _emit(handler, "app.main", logging.INFO, "should not raise")  # no-op, no crash
        assert handler._file_handler is None


# ---------------------------------------------------------------------------
# Querying back
# ---------------------------------------------------------------------------
def test_query_logs_returns_most_recent_first(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.a", logging.INFO, "first")
    _emit(handler, "app.b", logging.INFO, "second")
    _emit(handler, "app.c", logging.INFO, "third")

    results = query_logs(str(tmp_path), backup_count=3, limit=10)
    assert [r["message"] for r in results] == ["third", "second", "first"]


def test_query_logs_filters_by_level(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.a", logging.INFO, "info line")
    _emit(handler, "app.a", logging.ERROR, "error line")

    results = query_logs(str(tmp_path), backup_count=3, level="ERROR")
    assert len(results) == 1
    assert results[0]["message"] == "error line"


def test_query_logs_filters_by_component_substring(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.lifecycle.rca", logging.INFO, "rca line")
    _emit(handler, "app.lifecycle.remediation", logging.INFO, "remediation line")

    results = query_logs(str(tmp_path), backup_count=3, component="rca")
    assert len(results) == 1
    assert results[0]["message"] == "rca line"


def test_query_logs_filters_by_incident_id(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.a", logging.INFO, "for INC-1", incident_id="INC-1")
    _emit(handler, "app.a", logging.INFO, "for INC-2", incident_id="INC-2")

    results = query_logs(str(tmp_path), backup_count=3, incident_id="INC-1")
    assert len(results) == 1
    assert results[0]["message"] == "for INC-1"


def test_query_logs_free_text_search_matches_message(tmp_path):
    handler = _make_handler(tmp_path)
    _emit(handler, "app.a", logging.INFO, "rolling back citizen-service")
    _emit(handler, "app.a", logging.INFO, "scaling up frontend")

    results = query_logs(str(tmp_path), backup_count=3, search="citizen-service")
    assert len(results) == 1
    assert "citizen-service" in results[0]["message"]


def test_query_logs_respects_limit(tmp_path):
    handler = _make_handler(tmp_path)
    for i in range(5):
        _emit(handler, "app.a", logging.INFO, f"line {i}")

    results = query_logs(str(tmp_path), backup_count=3, limit=2)
    assert len(results) == 2
