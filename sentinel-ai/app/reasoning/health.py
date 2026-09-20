"""
Reasoner health — a provider failure is a *condition*, never an incident.

Why this exists
---------------
A failed LLM call (Gemini 404, a Groq rate limit, a DNS blip) used to leave
exactly one trace: `hypothesis.llm_note`. That is honest but incomplete:

  * nothing said *why* (endpoint, model, status, provider message) in a place
    an operator could query;
  * every incident, every cycle, waited out the full provider timeout before
    falling back to rules;
  * nothing distinguished "the provider is down" from "the model had nothing
    to add", so a GUI could not show a controlled REASONER_UNAVAILABLE state.

This tracker is the smallest thing that fixes all three. It is plain in-process
state attached to a Reasoner (`reasoner.health`), updated by the concrete
implementations, read by `rca.enrich_with_llm`:

    healthy --N consecutive failures--> UNAVAILABLE (circuit open)
    UNAVAILABLE --cooldown elapsed--> next call is a probe
    probe ok --> healthy        probe fails --> UNAVAILABLE again

While UNAVAILABLE, RCA runs rules-only (a fully supported mode) WITHOUT waiting
on the provider. Crucially this state lives here and in logs/events — it is
never turned into an Incident, and it never touches any incident's lifecycle
state: the incident carries on (or escalates) exactly as it would with no LLM
configured. The LLM was always strictly additive; this makes its absence cheap.
"""
from __future__ import annotations

import threading
import time
from typing import Any

STATUS_HEALTHY = "healthy"
STATUS_UNAVAILABLE = "reasoner_unavailable"
STATUS_UNKNOWN = "unknown"  # never called yet


class ReasonerHealth:
    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        clock: Any = time.time,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self.consecutive_failures = 0
        self.total_failures = 0
        self.last_error: str | None = None
        self.last_status_code: int | None = None
        self.last_failure_at: float | None = None
        self.last_success_at: float | None = None
        self._open_until: float = 0.0

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.last_success_at = self._clock()
            self._open_until = 0.0

    def record_failure(self, detail: str, status_code: int | None = None) -> None:
        with self._lock:
            now = self._clock()
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_error = (detail or "unknown error")[:500]
            self.last_status_code = status_code
            self.last_failure_at = now
            if self.consecutive_failures >= self.failure_threshold:
                # (Re)open the circuit. A failed probe re-arms the full
                # cooldown rather than hammering a provider that is down.
                self._open_until = now + self.cooldown_seconds

    def circuit_open(self) -> bool:
        """True while calls should be skipped."""
        with self._lock:
            return self._clock() < self._open_until

    def _status_locked(self, now: float) -> str:
        if now < self._open_until:
            return STATUS_UNAVAILABLE
        if self.last_success_at is None and self.total_failures == 0:
            return STATUS_UNKNOWN
        return STATUS_HEALTHY if self.consecutive_failures == 0 else STATUS_UNAVAILABLE

    @property
    def status(self) -> str:
        with self._lock:
            return self._status_locked(self._clock())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            return {
                "status": self._status_locked(now),
                "consecutive_failures": self.consecutive_failures,
                "total_failures": self.total_failures,
                "last_error": self.last_error,
                "last_status_code": self.last_status_code,
                "last_failure_at": self.last_failure_at,
                "last_success_at": self.last_success_at,
                "retry_after_seconds": max(0.0, self._open_until - now),
            }
