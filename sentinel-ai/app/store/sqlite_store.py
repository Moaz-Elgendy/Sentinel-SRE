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

-- Registered remote environments (Phase 1: usually exactly one row). The
-- full record — including connector secrets — is stored as JSON, same
-- schema-lite reasoning as `incidents.body`. Secrets never leave this table
-- via the API: app/domain/environment.py:Environment.to_public_dict() is the
-- only thing routers/environments.py is allowed to return.
CREATE TABLE IF NOT EXISTS environments (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_environments_customer ON environments(customer_id);

-- Sentinel SRE Control Center admins. Separate identity space from
-- citizen-service's `citizens` table (different database entirely) and from
-- CHAOS_ADMIN_TOKEN (a single shared secret, not a per-person account) — see
-- app/core/config.py's module notes on the GUI auth settings. `password_hash`
-- is bcrypt via passlib, never plaintext, never logged.
CREATE TABLE IF NOT EXISTS admins (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'sre_admin',
    created_at      REAL NOT NULL,
    last_login_at   REAL
);

-- SRE feedback on a diagnosis or a remediation (GUI spec sections 5/6).
-- Append-only on purpose: a re-opened or re-investigated incident can
-- collect more than one round of feedback over time, and each round is
-- evidence for the "Sentinel Performance" page (routers/performance.py) —
-- overwriting would destroy that history. This is data collection for a
-- FUTURE evaluation/improvement step, not a live retraining signal; nothing
-- in Sentinel reads this table to change its own behavior.
CREATE TABLE IF NOT EXISTS incident_feedback (
    id                  TEXT PRIMARY KEY,
    incident_id         TEXT NOT NULL,
    kind                TEXT NOT NULL,   -- 'diagnosis' | 'remediation'
    correct_or_useful   INTEGER NOT NULL,
    corrected_value     TEXT,            -- a RootCause or RemediationAction value
    note                TEXT,
    admin_id            TEXT NOT NULL,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_incident ON incident_feedback(incident_id);
CREATE INDEX IF NOT EXISTS idx_feedback_kind ON incident_feedback(kind);
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

    # ---- environments -----------------------------------------------------
    def upsert_environment(self, record: dict[str, Any]) -> None:
        """Insert or replace an environment by id.

        `record` is the FULL environment dict (secrets included) from
        `Environment.to_dict()` — never `to_public_dict()`. This is the one
        place those secrets are allowed to be written to disk; guard the
        caller, not this method.
        """
        conn = self._require()
        row = (
            record["id"],
            record["customer_id"],
            record["name"],
            record.get("created_at", time.time()),
            json.dumps(record, default=str),
        )
        with self._lock:
            conn.execute(
                """
                INSERT INTO environments (id, customer_id, name, created_at, body)
                VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    customer_id=excluded.customer_id,
                    name=excluded.name,
                    body=excluded.body
                """,
                row,
            )
            conn.commit()

    def get_environment(self, environment_id: str) -> dict[str, Any] | None:
        conn = self._require()
        with self._lock:
            cur = conn.execute(
                "SELECT body FROM environments WHERE id = ?", (environment_id,)
            )
            row = cur.fetchone()
        return json.loads(row["body"]) if row else None

    def list_environments(self) -> list[dict[str, Any]]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT body FROM environments ORDER BY created_at ASC"
            ).fetchall()
        return [json.loads(r["body"]) for r in rows]

    # ---- admins (Sentinel SRE Control Center auth) -----------------------
    def count_admins(self) -> int:
        """Used at startup to decide whether to bootstrap the first admin."""
        conn = self._require()
        with self._lock:
            row = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()
        return int(row["n"]) if row else 0

    def create_admin(
        self, admin_id: str, username: str, password_hash: str, role: str = "sre_admin"
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                """
                INSERT INTO admins (id, username, password_hash, role, created_at)
                VALUES (?,?,?,?,?)
                """,
                (admin_id, username, password_hash, role, time.time()),
            )
            conn.commit()

    def get_admin_by_username(self, username: str) -> dict[str, Any] | None:
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM admins WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def get_admin_by_id(self, admin_id: str) -> dict[str, Any] | None:
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM admins WHERE id = ?", (admin_id,)
            ).fetchone()
        return dict(row) if row else None

    def touch_admin_login(self, admin_id: str) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                "UPDATE admins SET last_login_at = ? WHERE id = ?",
                (time.time(), admin_id),
            )
            conn.commit()

    # ---- analytics (Sentinel SRE Control Center GUI) ----------------------
    def list_all_incidents_for_analytics(self) -> list[dict[str, Any]]:
        """Every incident, full body, no pagination.

        Used only by read-only aggregate endpoints (dashboard/actions/
        performance) that need to scan the whole history — never exposed
        directly as an API response. `GET /api/incidents` (routers/
        incidents.py) remains the paginated, list-trimmed endpoint for
        browsing; this exists so the GUI's summary views don't need to page
        through everything themselves to compute a count or an average.
        Sentinel's demo/prototype scale (hundreds, not millions, of
        incidents) makes an unpaginated scan fine; revisit if that changes.
        """
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT body FROM incidents ORDER BY created_at DESC"
            ).fetchall()
        return [json.loads(r["body"]) for r in rows]

    # ---- incident feedback (Sentinel SRE Control Center GUI) --------------
    def create_feedback(
        self,
        feedback_id: str,
        incident_id: str,
        kind: str,
        correct_or_useful: bool,
        corrected_value: str | None,
        note: str | None,
        admin_id: str,
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                """
                INSERT INTO incident_feedback
                    (id, incident_id, kind, correct_or_useful, corrected_value, note, admin_id, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    feedback_id,
                    incident_id,
                    kind,
                    1 if correct_or_useful else 0,
                    corrected_value,
                    note,
                    admin_id,
                    time.time(),
                ),
            )
            conn.commit()

    def list_feedback_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM incident_feedback WHERE incident_id = ? ORDER BY created_at ASC",
                (incident_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_feedback_for_analytics(self) -> list[dict[str, Any]]:
        """Every feedback row, unpaginated — same reasoning as
        `list_all_incidents_for_analytics`: only read by
        routers/performance.py to compute an aggregate, never returned
        directly as an API response."""
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM incident_feedback ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]
