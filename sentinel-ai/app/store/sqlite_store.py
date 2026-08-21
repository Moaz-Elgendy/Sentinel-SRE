"""
Incident history persistence.

SQLite, stdlib only. Why not Postgres, when there are already two Postgres
instances in the cluster? Because Sentinel must keep working when Postgres is
the thing that broke. An SRE agent whose incident store shares a failure
domain with the services it watches is useless in exactly the incident you
most want it for. SQLite on a PVC has no dependency on anything Sentinel
observes.

Concurrency model: one connection, guarded by a lock, `check_same_thread=False`
so the FastAPI threadpool can use it. Sentinel's write rate is a handful of
rows per incident, so this is not a bottleneck, and it avoids the
"database is locked" class of bug entirely.

The incident body is stored as a single JSON blob plus a few extracted
columns for querying. This is a deliberate schema-lite choice: the shape of
an incident record will change as the lifecycle evolves, and a JSON column
means that does not need a migration. The extracted columns are only the ones
the API filters/sorts on.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    alertname       TEXT NOT NULL,
    app             TEXT,
    severity        TEXT,
    status          TEXT NOT NULL,
    phase           TEXT,
    root_cause      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    resolved_at     REAL,
    escalated       INTEGER NOT NULL DEFAULT 0,
    body            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

-- Outcome ledger for the LEARNING phase. One row per (root cause, action)
-- pair attempted, with whether recovery validation subsequently passed.
-- Deliberately separate from `incidents`: it is an aggregate the decision
-- engine reads on the hot path, and joining/parsing JSON blobs for that
-- would be silly.
CREATE TABLE IF NOT EXISTS action_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT NOT NULL,
    root_cause    TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    succeeded     INTEGER NOT NULL,
    validated     INTEGER NOT NULL,
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_lookup ON action_outcomes(root_cause, action);
"""


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Open the DB, creating the parent directory if needed.

        Falls back to an in-memory database if the configured path is not
        writable (e.g. no PVC mounted at /data). Sentinel then works fully
        but forgets everything on restart — degraded LEARNING, not a crash.
        We log that loudly because silently losing the audit trail would be
        the worst of both worlds.
        """
        target = self.path
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                "sentinel_store_fallback_in_memory",
                extra={"configured_path": self.path, "error_detail": str(exc)[:200]},
            )
            target = ":memory:"
            conn = sqlite3.connect(":memory:", check_same_thread=False)

        conn.row_factory = sqlite3.Row
        # WAL keeps a long-running read (the /api/incidents list) from
        # blocking the lifecycle's writes.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # :memory: does not support WAL; harmless.
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        logger.info("sentinel_store_ready", extra={"db_path": target})

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStore.connect() was not called")
        return self._conn

    # ---- incidents ------------------------------------------------------
    def upsert_incident(self, record: dict[str, Any]) -> None:
        """Insert or replace an incident by id.

        Called after every lifecycle phase, so a pod eviction mid-incident
        leaves a partial-but-truthful record rather than nothing.
        """
        conn = self._require()
        hypothesis = record.get("hypothesis") or {}
        row = (
            record["id"],
            record["fingerprint"],
            record["alertname"],
            record.get("app"),
            record.get("severity"),
            record.get("status"),
            record.get("phase"),
            hypothesis.get("root_cause"),
            record.get("created_at"),
            record.get("updated_at"),
            record.get("resolved_at"),
            1 if record.get("escalated") else 0,
            json.dumps(record, default=str),
        )
        with self._lock:
            conn.execute(
                """
                INSERT INTO incidents
                    (id, fingerprint, alertname, app, severity, status, phase,
                     root_cause, created_at, updated_at, resolved_at, escalated, body)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    phase=excluded.phase,
                    root_cause=excluded.root_cause,
                    updated_at=excluded.updated_at,
                    resolved_at=excluded.resolved_at,
                    escalated=excluded.escalated,
                    body=excluded.body
                """,
                row,
            )
            conn.commit()

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        conn = self._require()
        with self._lock:
            cur = conn.execute("SELECT body FROM incidents WHERE id = ?", (incident_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row["body"])

    def list_incidents(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._require()
        sql = "SELECT body FROM incidents"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(r["body"]) for r in rows]

    def find_open_by_fingerprint(
        self, fingerprint: str, within_seconds: float
    ) -> dict[str, Any] | None:
        """Dedup lookup: the newest non-terminal incident for a fingerprint.

        `within_seconds` bounds it so that a stale incident nobody ever
        closed does not swallow a genuinely new outage weeks later.
        """
        conn = self._require()
        cutoff = time.time() - within_seconds
        with self._lock:
            row = conn.execute(
                """
                SELECT body FROM incidents
                 WHERE fingerprint = ?
                   AND status NOT IN ('resolved', 'escalated', 'auto_resolved')
                   AND updated_at >= ?
                 ORDER BY updated_at DESC LIMIT 1
                """,
                (fingerprint, cutoff),
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def count_open(self) -> int:
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM incidents "
                "WHERE status NOT IN ('resolved','escalated','auto_resolved')"
            ).fetchone()
        return int(row["n"]) if row else 0

    # ---- learning -------------------------------------------------------
    def record_outcome(
        self,
        incident_id: str,
        root_cause: str,
        action: str,
        target: str | None,
        succeeded: bool,
        validated: bool,
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                """
                INSERT INTO action_outcomes
                    (incident_id, root_cause, action, target, succeeded, validated, at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    incident_id,
                    root_cause,
                    action,
                    target,
                    1 if succeeded else 0,
                    1 if validated else 0,
                    time.time(),
                ),
            )
            conn.commit()

    def action_stats(self, root_cause: str) -> dict[str, dict[str, int]]:
        """Historical success per action for a root cause.

        Returns ``{action: {"attempts": n, "validated": m}}``. The Decision
        Engine uses this to reorder candidates — see learning.py for why it
        only reorders and never invents or unlocks an action.
        """
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                """
                SELECT action,
                       COUNT(*) AS attempts,
                       SUM(validated) AS validated
                  FROM action_outcomes
                 WHERE root_cause = ?
                 GROUP BY action
                """,
                (root_cause,),
            ).fetchall()
        return {
            r["action"]: {
                "attempts": int(r["attempts"] or 0),
                "validated": int(r["validated"] or 0),
            }
            for r in rows
        }
