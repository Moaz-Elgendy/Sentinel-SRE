"""
IncidentManager — incident identity, correlation and controlled concurrency.

    incoming alert
          |
          v
    IncidentManager.handle_alert()      <- deterministic; no LLM anywhere near it
          |
          +-- no incident for this identity ........ CREATE  -> lifecycle task
          +-- live incident (running) ............... JOIN    (counters only)
          +-- ESCALATED incident .................... JOIN, and at most schedule a
          |                                          bounded, evidence-gated
          |                                          reconsideration
          +-- RESOLVED, inside recurrence gap ....... ABSORB  (tail of the same
          |                                          occurrence)
          +-- RESOLVED, after the gap ............... CREATE  (new occurrence,
                                                     linked to the previous one)

Each incident then runs its OWN lifecycle as its own asyncio task:

    Incident A ---> lifecycle A
    Incident B ---> lifecycle B        (concurrent, isolated)

What is shared, and how it stays safe
-------------------------------------
* **Persistence is the source of truth.** Every state change is written to
  SQLite by the code that made it. The registry below only records which
  incidents have a lifecycle running in *this* process, so a restart loses
  nothing that matters (see `recover_interrupted`).
* **One writer per incident.** `_live[id]` holds the single in-memory
  `Incident` object whose lifecycle is running. Repeat alerts are folded into
  THAT object (never into a stale copy read back from the database), and the
  authorization / re-run paths take the same lease. Without this the old
  design lost updates: the webhook read a copy, bumped a counter and saved the
  whole record, then the orchestrator's next save overwrote it.
* **SQLite.** One connection, one lock, WAL (see store/sqlite_store.py). All
  writes from all lifecycles are serialised by that lock and each is a short
  synchronous statement, so concurrent lifecycles cannot corrupt the database
  or hit "database is locked". Whole-record `upsert` is last-writer-wins *per
  incident*, which is why the single-writer rule above matters.
* **Remediation is serialised per Deployment** by the orchestrator, and the
  Policy Engine's cooldown now sees actions taken by OTHER incidents.

Known limitations (deliberate, documented in docs/incident-engine.md)
--------------------------------------------------------------------
* In-process only: correct for the single-replica deployment Sentinel uses.
  Two replicas would each keep their own registry; the fix then is a lease row
  in the store, not a different design.
* A restart kills running lifecycles. `recover_interrupted` resumes a
  lifecycle that had done nothing irreversible (once) and otherwise ESCALATES
  it as INTERRUPTED rather than blindly repeating a possibly half-applied
  action.
* `max_concurrent_lifecycles` bounds concurrency; extra incidents wait,
  visibly, in status OPEN.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.lifecycle import detection
from app.models.incident import (
    AUTO_RECONSIDERABLE_REASONS,
    EscalationReason,
    Incident,
    IncidentStatus,
    LifecyclePhase,
)

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (
    IncidentStatus.OPEN.value,
    IncidentStatus.INVESTIGATING.value,
    IncidentStatus.REMEDIATING.value,
    IncidentStatus.VALIDATING.value,
)
_CLOSED_STATUSES = (IncidentStatus.RESOLVED.value, IncidentStatus.AUTO_RESOLVED.value)


class CorrelationKind(str, Enum):
    CREATED = "created"                      # brand-new problem
    RECURRENCE = "recurrence"                # new occurrence after a closed one
    JOINED_ACTIVE = "joined_active"          # repeat of a running incident
    JOINED_ESCALATED = "joined_escalated"    # repeat of an escalated incident
    ABSORBED_RESOLVED = "absorbed_resolved"  # tail of a just-resolved occurrence


@dataclass
class CorrelationDecision:
    kind: CorrelationKind
    incident_id: str
    reason: str
    incident: Incident | None = None   # set only when a NEW incident was created
    reconsideration_scheduled: bool = False

    @property
    def created(self) -> bool:
        return self.kind in (CorrelationKind.CREATED, CorrelationKind.RECURRENCE)


class IncidentManager:
    def __init__(
        self,
        store: Any,
        settings: Any,
        event_bus: Any = None,
        orchestrator: Any = None,
        clock: Any = time.time,
    ) -> None:
        self.store = store
        self.settings = settings
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self._clock = clock
        self._lock = threading.RLock()
        self._live: dict[str, Incident] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------
    # leases: "a lifecycle (or authorised action) is running for this id"
    # ------------------------------------------------------------------
    def try_acquire(self, incident: Incident) -> bool:
        """Take the single-writer lease. False if one is already held."""
        with self._lock:
            if incident.id in self._live:
                return False
            self._live[incident.id] = incident
            return True

    def release(self, incident_id: str) -> None:
        with self._lock:
            self._live.pop(incident_id, None)

    def is_running(self, incident_id: str) -> bool:
        with self._lock:
            return incident_id in self._live

    def running_ids(self) -> list[str]:
        with self._lock:
            return list(self._live)

    # ------------------------------------------------------------------
    # events (real, from the backend — nothing here is decorative)
    # ------------------------------------------------------------------
    def emit(
        self,
        incident: Incident | dict[str, Any],
        message: str,
        kind: str,
        **extra: Any,
    ) -> None:
        """Log + publish one activity line for Sentinel Live.

        Published as `incident_updated` (the event type the GUI already
        subscribes to) so it shows up with no frontend change. Like every
        EventBus publish it can never affect processing.
        """
        data = incident.to_dict(include_evidence=False) if isinstance(incident, Incident) else incident
        logger.info(
            "incident_activity",
            # NOT `incident_id`: inside a lifecycle task the logging record
            # factory already sets that attribute from a ContextVar, and
            # logging raises KeyError if `extra` tries to overwrite it.
            extra={
                "incident_ref": data.get("id"),
                "event_kind": kind,
                "activity": message,
                **{k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool, type(None)))},
            },
        )
        if self.event_bus is None:
            return
        try:
            self.event_bus.publish(
                {
                    "type": "incident_updated",
                    "incident_id": data.get("id"),
                    "alertname": data.get("alertname"),
                    "severity": data.get("severity"),
                    "phase": data.get("phase"),
                    "status": data.get("status"),
                    "message": message,
                    "event_kind": kind,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("incident_event_publish_failed", extra={"error_detail": str(exc)[:200]})

    # ------------------------------------------------------------------
    # webhook entry points (synchronous on purpose: no await between
    # "look at the current state" and "change it", so two alerts handled by
    # the same event loop can never interleave inside a decision)
    # ------------------------------------------------------------------
    def handle_alert(
        self, normalised: dict[str, Any], environment: Any = None
    ) -> CorrelationDecision:
        now = self._clock()
        ident = detection.identity_for(normalised, environment)
        key = ident["fingerprint"]
        alertname = normalised["alertname"]
        app = normalised.get("app")

        with self._lock:
            latest = self.store.find_latest_by_fingerprint(key)
            self._emit_received(normalised, latest)

            if latest is None:
                return self._create(normalised, environment, now)

            status = latest.get("status")
            live = self._live.get(latest["id"])

            # ---- a lifecycle is running for it -------------------------
            if live is not None:
                return self._join_running(live, normalised, now)

            if status in _ACTIVE_STATUSES:
                # Active in the database but nothing is running it: the
                # process restarted (or a lifecycle died) after startup
                # recovery. Never resume silently from a webhook; close it
                # out as INTERRUPTED so it becomes an ordinary escalated
                # incident that this repeat can then join.
                incident = Incident.from_dict(latest)
                self._interrupt(incident, "found active with no running lifecycle")
                latest = incident.to_dict()
                status = latest["status"]

            # ---- escalated: controlled terminal state -------------------
            if status == IncidentStatus.ESCALATED.value:
                return self._join_escalated(latest, normalised, now)

            # ---- resolved / auto_resolved ------------------------------
            if status in _CLOSED_STATUSES:
                gap = float(getattr(self.settings, "incident_recurrence_gap_seconds", 600))
                closed_at = latest.get("resolved_at") or latest.get("updated_at") or 0.0
                if now - closed_at < gap:
                    return self._absorb_after_resolution(latest, normalised, now, gap)
                return self._create(normalised, environment, now, previous=latest)

            # Unknown status value (future-proofing): treat as new rather than
            # swallowing a real alert.
            logger.warning(
                "correlation_unknown_status",
                extra={"status": status, "alertname": alertname, "app": app},
            )
            return self._create(normalised, environment, now, previous=latest)

    def handle_resolved(self, normalised: dict[str, Any], environment: Any = None) -> dict[str, Any]:
        """Alertmanager says the condition cleared."""
        key = detection.identity_for(normalised, environment)["fingerprint"]
        alertname = normalised["alertname"]
        now = self._clock()
        with self._lock:
            latest = self.store.find_latest_by_fingerprint(key)
            if latest is None or latest.get("status") in _CLOSED_STATUSES:
                return {
                    "alertname": alertname,
                    "reason": "resolved notification with no matching open incident",
                }
            live = self._live.get(latest["id"])
            note = (
                "Alertmanager reported the alert as resolved. The incident is closed "
                "as auto_resolved: the condition cleared on its own or by someone "
                "else's action, NOT as a verified result of a Sentinel remediation. "
                "Counting this as a Sentinel success would corrupt both the "
                "effectiveness metrics and the learning bias."
            )
            if live is not None:
                # A lifecycle is mid-flight and owns this record. Note it and
                # let validation decide; closing it from here would be
                # overwritten by the lifecycle's next save anyway.
                live.record(
                    LifecyclePhase.DETECTION,
                    "Alertmanager reported the alert as resolved while this lifecycle "
                    "is still running; recovery validation will decide the outcome.",
                )
                self.store.upsert_incident(live.to_dict())
                self.emit(live, "Alertmanager reported the alert resolved; validation continues", "alert_resolved")
                return {
                    "alertname": alertname,
                    "incident_id": live.id,
                    "reason": "noted on the running incident; lifecycle continues",
                }
            incident = Incident.from_dict(latest)
            incident.status = IncidentStatus.AUTO_RESOLVED
            incident.resolved_at = now
            incident.record(LifecyclePhase.DETECTION, note)
            self.store.upsert_incident(incident.to_dict())
            self.emit(incident, "Incident auto-resolved: Alertmanager reported the alert resolved", "auto_resolved")
            logger.info(
                "incident_auto_resolved",
                extra={"incident_id": incident.id, "alertname": alertname},
            )
            return {
                "alertname": alertname,
                "incident_id": incident.id,
                "reason": "marked auto_resolved from an Alertmanager resolved notification",
            }

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------
    def _emit_received(self, normalised: dict[str, Any], latest: dict[str, Any] | None) -> None:
        logger.info(
            "alert_received",
            extra={
                "alertname": normalised["alertname"],
                "app": normalised.get("app"),
                "existing_incident_id": (latest or {}).get("id"),
            },
        )

    def _create(
        self,
        normalised: dict[str, Any],
        environment: Any,
        now: float,
        previous: dict[str, Any] | None = None,
    ) -> CorrelationDecision:
        incident = detection.build_incident(normalised, environment)
        if previous is not None:
            incident.occurrence = int(previous.get("occurrence", 1)) + 1
            incident.previous_incident_id = previous.get("id")
            incident.record(
                LifecyclePhase.DETECTION,
                f"new occurrence #{incident.occurrence} of a previously closed problem "
                f"(previous incident {previous.get('id')}, status {previous.get('status')}); "
                "opened as a NEW incident instead of reopening history",
                previous_incident_id=previous.get("id"),
            )
        incident.last_seen_at = now
        self.store.upsert_incident(incident.to_dict())
        self.try_acquire(incident)
        kind = CorrelationKind.RECURRENCE if previous is not None else CorrelationKind.CREATED
        self.emit(
            incident,
            f"Created incident {incident.id} for {incident.alertname} on {incident.app or 'unknown app'}"
            + (f" (occurrence #{incident.occurrence})" if previous is not None else ""),
            "created",
        )
        logger.info(
            "incident_opened",
            extra={
                "incident_id": incident.id,
                "alertname": incident.alertname,
                "app": incident.app,
                "severity": incident.severity.value,
                "incident_key": incident.fingerprint,
                "occurrence": incident.occurrence,
            },
        )
        self._spawn(self._run_lifecycle(incident), name=f"lifecycle-{incident.id}")
        return CorrelationDecision(
            kind=kind,
            incident_id=incident.id,
            reason="new incident opened",
            incident=incident,
        )

    def _fold_repeat(self, incident: Incident, normalised: dict[str, Any], now: float) -> None:
        """Counters only. A repeat never restarts, re-plans or re-decides."""
        incident.firing_count += 1
        incident.suppressed_repeats += 1
        incident.last_seen_at = now
        am_fp = normalised.get("fingerprint")
        if am_fp and am_fp not in incident.alert_fingerprints:
            incident.alert_fingerprints.append(am_fp)
            incident.alert_fingerprints[:] = incident.alert_fingerprints[-20:]
        # Timeline gets the first repeats and then every 10th; every repeat is
        # still counted and still emitted live. An unresolved alert repeating
        # for days must not bloat the audit trail.
        if incident.suppressed_repeats in (1, 5) or incident.suppressed_repeats % 10 == 0:
            incident.record(
                LifecyclePhase.DETECTION,
                f"repeat firing #{incident.firing_count} for the same problem correlated "
                f"into this incident ({incident.suppressed_repeats} repeat(s) absorbed, "
                "no new lifecycle started)",
                alertname=normalised["alertname"],
                pod=normalised.get("pod"),
            )

    def _join_running(
        self, live: Incident, normalised: dict[str, Any], now: float
    ) -> CorrelationDecision:
        self._fold_repeat(live, normalised, now)
        self.store.upsert_incident(live.to_dict())
        msg = (
            f"Duplicate alert correlated with {live.id} (status {live.status.value}); "
            "no new investigation or remediation started"
        )
        self.emit(live, msg, "duplicate_correlated")
        return CorrelationDecision(
            kind=CorrelationKind.JOINED_ACTIVE, incident_id=live.id, reason=msg
        )

    def _join_escalated(
        self, latest: dict[str, Any], normalised: dict[str, Any], now: float
    ) -> CorrelationDecision:
        incident = Incident.from_dict(latest)
        self._fold_repeat(incident, normalised, now)

        reason_val = incident.escalation_reason
        max_reopens = int(getattr(self.settings, "max_incident_reopens", 3))
        min_gap = float(getattr(self.settings, "escalated_reconsider_min_interval_seconds", 600))
        scheduled = False
        why = "escalated incident is waiting for a human or for materially new evidence"

        if incident.action_count > 0:
            # Sentinel already changed something on this incident's behalf and
            # it did not work. Whatever the escalation reason says, more
            # automatic attempts are not the answer: a human decides.
            why = (
                f"escalated after {incident.action_count} executed action(s) that did not "
                "resolve it; waiting for a human"
            )
        elif reason_val not in AUTO_RECONSIDERABLE_REASONS:
            why = (
                f"escalated ({reason_val.value if reason_val else 'unknown'}): Sentinel has "
                "exhausted what it may do automatically; waiting for a human"
            )
        elif incident.reopen_count >= max_reopens:
            why = (
                f"re-investigation blocked by retry limit ({incident.reopen_count}/"
                f"{max_reopens} automatic reopens used); waiting for a human"
            )
            if not incident.escalation_record.get("reopen_blocked_noted"):
                incident.escalation_record["reopen_blocked_noted"] = True
                incident.record(LifecyclePhase.ESCALATION, why, reason="retry_limit")
            self.emit(incident, "Re-investigation blocked by retry limit", "reinvestigation_blocked")
        elif (
            incident.last_reconsidered_at is not None
            and now - incident.last_reconsidered_at < min_gap
        ):
            why = "escalated; a re-check ran recently, not repeating it yet"
        elif self.orchestrator is not None and self.try_acquire(incident):
            scheduled = True
            self._spawn(self._run_reconsideration(incident), name=f"reconsider-{incident.id}")
            why = "escalated; scheduled ONE evidence re-check (reopens only if evidence changed materially)"

        self.store.upsert_incident(incident.to_dict())
        self.emit(
            incident,
            "Duplicate alert correlated with escalated incident "
            f"{incident.id}; not escalating again",
            "duplicate_correlated_escalated",
        )
        return CorrelationDecision(
            kind=CorrelationKind.JOINED_ESCALATED,
            incident_id=incident.id,
            reason=why,
            reconsideration_scheduled=scheduled,
        )

    def _absorb_after_resolution(
        self, latest: dict[str, Any], normalised: dict[str, Any], now: float, gap: float
    ) -> CorrelationDecision:
        incident = Incident.from_dict(latest)
        incident.firing_count += 1
        incident.suppressed_repeats += 1
        incident.last_seen_at = now
        self.store.upsert_incident(incident.to_dict())
        msg = (
            f"Alert fired again {now - (latest.get('resolved_at') or now):.0f}s after "
            f"{incident.id} closed; inside the {gap:.0f}s recurrence gap this is the tail of "
            "the same occurrence, so no new incident was opened"
        )
        self.emit(incident, "Duplicate alert ignored (tail of a just-resolved incident)", "duplicate_ignored")
        return CorrelationDecision(
            kind=CorrelationKind.ABSORBED_RESOLVED, incident_id=incident.id, reason=msg
        )

    # ------------------------------------------------------------------
    # task scheduling
    # ------------------------------------------------------------------
    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(
                max(1, int(getattr(self.settings, "max_concurrent_lifecycles", 4)))
            )
        return self._semaphore

    async def _run_lifecycle(self, incident: Incident) -> None:
        """Run one incident's lifecycle. Always releases the lease."""
        try:
            sem = self._sem()
            if sem.locked():
                self.emit(incident, "Waiting for a free lifecycle slot", "queued")
            async with sem:
                await self.orchestrator.run(incident)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - orchestrator.run never raises; belt and braces
            logger.exception("lifecycle_task_failed", extra={"incident_id": incident.id})
        finally:
            self.release(incident.id)

    async def _run_reconsideration(self, incident: Incident, forced: bool = False, actor: str | None = None) -> None:
        try:
            async with self._sem():
                await self.orchestrator.reconsider(incident, forced=forced, actor=actor)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("reconsideration_task_failed", extra={"incident_id": incident.id})
        finally:
            self.release(incident.id)

    def start_manual_reinvestigation(self, incident: Incident, actor: str) -> bool:
        """Human-initiated re-run of an ESCALATED incident (admin API).
        Returns False if a lifecycle already holds the incident."""
        if not self.try_acquire(incident):
            return False
        self._spawn(
            self._run_reconsideration(incident, forced=True, actor=actor),
            name=f"reinvestigate-{incident.id}",
        )
        return True

    # ------------------------------------------------------------------
    # restart recovery
    # ------------------------------------------------------------------
    def _interrupt(self, incident: Incident, why: str) -> None:
        """Close an orphaned in-flight incident as ESCALATED/INTERRUPTED."""
        incident.escalated = True
        incident.escalation_reason = EscalationReason.INTERRUPTED
        incident.escalation_detail = (
            f"Sentinel stopped while this incident was {incident.status.value} "
            f"(phase {incident.phase.value}); {why}. "
            + (
                f"{incident.action_count} remediation action(s) had already been executed, "
                "so Sentinel will not repeat them blindly: a human should verify the "
                "current state."
                if incident.action_count
                else "No remediation had been executed."
            )
        )
        incident.status = IncidentStatus.ESCALATED
        incident.escalation_record = {
            "incident_id": incident.id,
            "at": self._clock(),
            "reason": EscalationReason.INTERRUPTED.value,
            "detail": incident.escalation_detail,
            "root_cause": incident.hypothesis.root_cause.value if incident.hypothesis else None,
            "confidence": incident.hypothesis.confidence if incident.hypothesis else None,
            "rejected_actions": [],
            "policy_reasons": [],
        }
        incident.record(LifecyclePhase.ESCALATION, incident.escalation_detail, reason="interrupted")
        self.store.upsert_incident(incident.to_dict())
        self.emit(incident, "Incident escalated: Sentinel restarted mid-lifecycle", "escalated_interrupted")

    def recover_interrupted(self) -> dict[str, list[str]]:
        """Startup: deal with incidents left in-flight by the previous process.

        * Nothing irreversible done yet (no executed action) and not resumed
          before -> resume the lifecycle ONCE.
        * Otherwise -> ESCALATED (INTERRUPTED), never re-run automatically.

        "Resumed before" is recorded on the timeline, so a crash-looping
        Sentinel cannot resume the same incident forever.
        """
        resumed: list[str] = []
        escalated: list[str] = []
        for record in self.store.list_by_statuses(_ACTIVE_STATUSES):
            if record["id"] in self._live:
                continue
            incident = Incident.from_dict(record)
            already_resumed = any(
                (e.detail or {}).get("resumed_after_restart") for e in incident.timeline
            )
            can_resume = (
                self.orchestrator is not None
                and incident.action_count == 0
                and not already_resumed
                and incident.status in (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)
            )
            if can_resume and self.try_acquire(incident):
                incident.record(
                    LifecyclePhase.DETECTION,
                    "Sentinel restarted before this incident finished investigating; no "
                    "remediation had been executed, so the lifecycle is resumed once from "
                    "the start",
                    resumed_after_restart=True,
                )
                incident.status = IncidentStatus.OPEN
                self.store.upsert_incident(incident.to_dict())
                self.emit(incident, "Resuming interrupted investigation after restart", "resumed")
                self._spawn(self._run_lifecycle(incident), name=f"resume-{incident.id}")
                resumed.append(incident.id)
            else:
                self._interrupt(incident, "recovered at startup")
                escalated.append(incident.id)
        if resumed or escalated:
            logger.warning(
                "interrupted_incidents_recovered",
                extra={"resumed": resumed, "escalated": escalated},
            )
        return {"resumed": resumed, "escalated": escalated}

    async def shutdown(self) -> None:
        """Cancel running lifecycles. Their persisted state is already the
        truth; the next start's `recover_interrupted` classifies them."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
