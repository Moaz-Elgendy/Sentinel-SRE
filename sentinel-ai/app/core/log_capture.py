"""
Persistent storage + live streaming for Sentinel's OWN logs (the "Sentinel
Logs" GUI page — see routers/logs.py), independent of Loki.

Why not just query Loki for these, the way the GUI queries Loki for the
*application's* logs? Two reasons, both from the actual deployment
topology (see infra/terraform/sentinel_remote.tf): the external EC2
Sentinel runs as a plain Docker container, entirely outside the K3s
cluster, and Grafana Alloy (the log shipper — see k8s/monitoring/alloy/)
only scrapes pods *inside* that cluster. It has no visibility into this
box at all. So Sentinel's own logs are not in Loki, and even where Alloy
CAN reach Sentinel (the in-cluster topology), Sentinel must still be able
to show its own logs when Loki itself is down or unreachable — that is
precisely the situation an SRE needs Sentinel's logs to diagnose.

Design, deliberately simple:
  * A `logging.Handler` writes every log record as one JSON line to a
    rotating file on the SAME persistent volume as the SQLite incident
    store (co-located by default — see Settings.sentinel_log_dir_resolved)
    so it has the exact same "survives a restart" property, with no new
    volume/mount to configure.
  * Bounded by rotation (`maxBytes` x `backupCount`), never unbounded
    growth — this is deliberately NOT stored in SQLite (a growing,
    unindexed blob table would be a worse fit than a file for something
    that is fundamentally an append-only, time-ordered stream read in
    recent-first order).
  * The SAME handler optionally fans each line out to an in-process
    EventBus (see core/events.py — reusing that exact class, a second
    instance, so live tailing follows the identical "simple in-process
    pub/sub, swappable later" design already chosen for incident events)
    for `GET /api/logs/stream`'s live tail.
  * A `RedactionFilter` scrubs known secret shapes (API keys, bearer
    tokens, AWS keys, generic `key=value` secret-looking pairs) from both
    the message and any structured `extra` fields BEFORE either the file
    write or the bus publish — logs are the #1 place secrets leak by
    accident, and this is enforced at the one choke point every log line
    passes through, not left to every call site to remember.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
import time
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Keys whose values are dropped outright, wherever they show up in `extra`.
_SECRET_KEYS = {
    "openai_api_key", "gemini_api_key", "github_token", "slack_webhook_url",
    "chaos_admin_token", "kubernetes_token", "k8s_token", "kubeconfig",
    "aws_secret_access_key", "aws_session_token", "password", "password_hash",
    "authorization", "bearer_token", "jwt_secret", "sentinel_jwt_secret",
    "generated_password", "access_token", "refresh_token", "secret",
}

# Patterns scrubbed out of free-text message strings — bearer tokens, common
# vendor key shapes, and AWS access key ids. Deliberately pattern-based
# (rather than trying to enumerate every secret's exact value) so a secret
# that slips into a message via string interpolation is still caught.
_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.=]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens/webhooks
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
_REDACTED = "[REDACTED]"


def _redact_text(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: (_REDACTED if k.lower() in _SECRET_KEYS else _redact_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


class RedactionFilter(logging.Filter):
    """Scrubs secrets from a record's message and `extra` fields in place,
    before formatting. Applied to the persistent handler specifically (see
    configure_logging) — never raises, since a redaction bug must not be
    able to take down logging itself.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact_text(record.msg)
            for key, value in list(record.__dict__.items()):
                if key in ("msg", "args", "exc_info", "exc_text", "stack_info"):
                    continue
                if key.lower() in _SECRET_KEYS:
                    record.__dict__[key] = _REDACTED
                elif isinstance(value, (str, dict, list)):
                    record.__dict__[key] = _redact_value(value)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


# ---------------------------------------------------------------------------
# Persistent + live-streamed handler
# ---------------------------------------------------------------------------


class PersistentLogHandler(logging.Handler):
    """Writes redacted JSON-lines to a rotating file and, once a bus is
    attached (main.py does this once the app's `log_bus` exists — see that
    module's lifespan), fans the same redacted record out live for
    `GET /api/logs/stream`.

    Falls back to writing nothing (log-capture-only feature degrades, the
    stdout handler configure_logging always installs separately keeps
    working) if the directory is not writable, exactly like SQLiteStore's
    own fallback — this must never be able to crash startup.
    """

    def __init__(self, log_dir: str, max_bytes: int, backup_count: int) -> None:
        super().__init__()
        self._bus: Any = None
        self._file_handler: logging.handlers.RotatingFileHandler | None = None
        self.log_dir = log_dir
        self.log_path = str(Path(log_dir) / "sentinel.log")

        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                self.log_path, maxBytes=max_bytes, backupCount=backup_count
            )
            file_handler.setFormatter(formatter)
            self._file_handler = file_handler
        except OSError as exc:
            logging.getLogger(__name__).error(
                "sentinel_log_capture_disabled",
                extra={"configured_dir": log_dir, "error_detail": str(exc)[:200]},
            )

    def set_bus(self, bus: Any) -> None:
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        if self._file_handler is None:
            return
        try:
            self._file_handler.emit(record)
            if self._bus is not None:
                line = self._file_handler.format(record)
                payload = json.loads(line)
                self._bus.publish({"type": "log", **payload})
        except Exception:  # noqa: BLE001 - a broken log line must not raise
            self.handleError(record)


# ---------------------------------------------------------------------------
# Reading back for GET /api/logs
# ---------------------------------------------------------------------------

_read_lock = threading.Lock()


def _candidate_files(log_dir: str, backup_count: int) -> list[Path]:
    """Current file first is wrong for reverse-chronological reads — rotated
    backups (`.1`, `.2`, ...) hold OLDER lines than the live file, and
    within a single file, later lines are newer. Callers read the live file
    backward, then fall through to backups oldest-content-last."""
    base = Path(log_dir) / "sentinel.log"
    files = [base] if base.exists() else []
    for i in range(1, backup_count + 1):
        candidate = Path(f"{base}.{i}")
        if candidate.exists():
            files.append(candidate)
    return files


def query_logs(
    log_dir: str,
    backup_count: int,
    *,
    level: str | None = None,
    component: str | None = None,
    incident_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
    search: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read persisted log lines, most-recent-first, applying filters.

    Simple linear scan, newest file first and within each file from the
    end backward. Sentinel's own log volume (a handful of lines per
    incident, plus routine operational noise) never approaches a size
    where this needs an index — see this module's docstring for why a
    SQLite table was deliberately NOT used here.
    """
    results: list[dict[str, Any]] = []
    search_lower = search.lower() if search else None

    with _read_lock:
        for path in _candidate_files(log_dir, backup_count):
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for raw_line in reversed(lines):
                if not raw_line.strip():
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if level and entry.get("level", "").upper() != level.upper():
                    continue
                if component and component.lower() not in str(entry.get("name", "")).lower():
                    continue
                if incident_id and entry.get("incident_id") != incident_id:
                    continue
                entry_time = entry.get("timestamp_epoch")
                if since is not None and entry_time is not None and entry_time < since:
                    continue
                if until is not None and entry_time is not None and entry_time > until:
                    continue
                if search_lower and search_lower not in json.dumps(entry).lower():
                    continue

                results.append(entry)
                if len(results) >= limit:
                    return results
    return results
