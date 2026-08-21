from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock

from prometheus_client import Counter, Gauge


chaos_injections_total = Counter(
    "chaos_injections_total",
    "Total number of deliberately injected chaos faults",
    ["fault_type"],
)

chaos_latency_ms = Gauge(
    "chaos_latency_ms",
    "Currently configured artificial request latency in milliseconds",
)

chaos_error_rate = Gauge(
    "chaos_error_rate",
    "Currently configured forced HTTP 5xx error probability",
)

chaos_db_failure = Gauge(
    "chaos_db_failure",
    "Whether simulated database failure is currently enabled (1/0)",
)

chaos_notification_failure_rate = Gauge(
    "chaos_notification_failure_rate",
    "Currently configured simulated notification provider failure probability",
)


@dataclass(frozen=True)
class ChaosState:
    latency_ms: int = 0
    error_rate: float = 0.0
    db_failure: bool = False
    notification_failure_rate: float = 0.0


class ChaosController:
    def __init__(self, initial_notification_failure_rate: float = 0.0) -> None:
        self._lock = RLock()
        self._default_notification_failure_rate = initial_notification_failure_rate
        self._state = ChaosState(notification_failure_rate=initial_notification_failure_rate)
        self._publish()

    def set_default_notification_failure_rate(self, value: float) -> None:
        with self._lock:
            self._default_notification_failure_rate = value
            self._state = ChaosState(
                latency_ms=self._state.latency_ms,
                error_rate=self._state.error_rate,
                db_failure=self._state.db_failure,
                notification_failure_rate=value,
            )
            self._publish()

    def get(self) -> ChaosState:
        with self._lock:
            return replace(self._state)

    def update(
        self,
        *,
        latency_ms: int | None = None,
        error_rate: float | None = None,
        db_failure: bool | None = None,
        notification_failure_rate: float | None = None,
    ) -> ChaosState:
        with self._lock:
            self._state = ChaosState(
                latency_ms=self._state.latency_ms if latency_ms is None else latency_ms,
                error_rate=self._state.error_rate if error_rate is None else error_rate,
                db_failure=self._state.db_failure if db_failure is None else db_failure,
                notification_failure_rate=(
                    self._state.notification_failure_rate
                    if notification_failure_rate is None
                    else notification_failure_rate
                ),
            )
            self._publish()
            return replace(self._state)

    def reset(self) -> ChaosState:
        with self._lock:
            self._state = ChaosState(
                notification_failure_rate=self._default_notification_failure_rate
            )
            self._publish()
            return replace(self._state)

    def record(self, fault_type: str) -> None:
        chaos_injections_total.labels(fault_type=fault_type).inc()

    def _publish(self) -> None:
        chaos_latency_ms.set(self._state.latency_ms)
        chaos_error_rate.set(self._state.error_rate)
        chaos_db_failure.set(1 if self._state.db_failure else 0)
        chaos_notification_failure_rate.set(self._state.notification_failure_rate)


controller = ChaosController()
