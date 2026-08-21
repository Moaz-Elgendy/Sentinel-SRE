from __future__ import annotations

import time
from dataclasses import dataclass, replace
from threading import Event, RLock, Thread

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

chaos_cpu_burn = Gauge(
    "chaos_cpu_burn",
    "Whether the artificial CPU burn worker is currently enabled (1/0)",
)

chaos_memory_leak_mb = Gauge(
    "chaos_memory_leak_mb",
    "Approximate MiB currently retained by the simulated memory leak",
)


# ---------------------------------------------------------------------------
# CPU burn worker
# ---------------------------------------------------------------------------
# Why a duty cycle instead of a flat-out busy loop:
#
# The point of this fault is that *something else* — an operator, or Sentinel
# — observes the incident on `rate(process_cpu_seconds_total[2m])` and
# remediates it. That is only possible if the pod keeps answering /healthz,
# /readyz and /metrics while the burn is running. A 100%-busy Python loop
# holds the GIL in long stretches and would starve the uvicorn event loop
# badly enough that the kubelet's liveness probe could kill the pod before
# any alert is even evaluated — which would turn a CPU incident into a
# restart loop and destroy the very signal we are trying to produce.
#
# So the worker alternates: BUSY_SECONDS of arithmetic, then IDLE_SECONDS of
# sleep. sleep() releases the GIL unconditionally, guaranteeing the event
# loop a slice on every cycle. 20ms/5ms is an 80% duty cycle: high enough
# that rate(process_cpu_seconds_total) sits near 0.8 cores and trips any
# sane CPU alert threshold, while the 5ms gap (plus the interpreter's own
# 5ms GIL switch interval during the busy phase) keeps probe latency in the
# low tens of milliseconds. The slices are deliberately short so the worst
# case a probe can wait is one busy slice, not one whole second.
#
# Honest limitations, both worth knowing before reading the graph:
#  1. Because of the GIL, one burner thread in CPython can consume at most
#     ~1 core, and it competes with the request handlers for that same core.
#     This will not saturate a multi-core node — it saturates this process,
#     which is exactly what a process_cpu_seconds_total-based alert
#     measures, so it is the right signal for this project even though it is
#     not a true machine-level CPU incident.
#  2. The Deployment sets a 500m CPU limit, so the cgroup throttles this
#     container at half a core anyway. Expect
#     rate(process_cpu_seconds_total[2m]) to plateau near 0.5, not 0.8 — the
#     duty cycle is the ceiling we ask for, the limit is the ceiling we get.
#     A CPU alert threshold must therefore sit below 0.5 to ever fire here.
_CPU_BURN_BUSY_SECONDS = 0.020
_CPU_BURN_IDLE_SECONDS = 0.005


class _CpuBurner:
    """A single lazily-started daemon thread that burns CPU only while the
    flag is set. The thread is never torn down — it parks on Event.wait(),
    which costs nothing — because repeatedly creating and joining threads
    across many inject/reset cycles is a more fragile design than one
    long-lived worker that idles."""

    def __init__(self) -> None:
        self._enabled = Event()
        self._start_lock = RLock()
        self._thread: Thread | None = None

    def set(self, enabled: bool) -> None:
        if enabled:
            self._ensure_thread()
            self._enabled.set()
        else:
            self._enabled.clear()

    def is_running(self) -> bool:
        return self._enabled.is_set()

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                # daemon=True so a burning pod can still shut down cleanly on
                # SIGTERM; we never want this thread to block a rollout.
                self._thread = Thread(
                    target=self._run, name="chaos-cpu-burn", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            # Blocks (consuming nothing) until someone enables the fault.
            self._enabled.wait()
            deadline = time.perf_counter() + _CPU_BURN_BUSY_SECONDS
            counter = 0
            while time.perf_counter() < deadline:
                # Cheap integer arithmetic: enough to keep the CPU pegged
                # without allocating, so this fault does not accidentally
                # also look like a memory leak.
                counter = (counter * 31 + 17) % 1_000_003
            time.sleep(_CPU_BURN_IDLE_SECONDS)


_cpu_burner = _CpuBurner()


# ---------------------------------------------------------------------------
# Memory leak buffer
# ---------------------------------------------------------------------------
# Retained at module level so the allocation genuinely outlives the request
# that asked for it — a leak that a garbage collector can reclaim is not a
# leak and would never move process_resident_memory_bytes.
_MEMORY_CHUNK_MB = 8
_PAGE_SIZE = 4096
_memory_lock = RLock()
_leaked_chunks: list[bytearray] = []


def _leaked_mb() -> int:
    with _memory_lock:
        return sum(len(chunk) for chunk in _leaked_chunks) // (1024 * 1024)


def _resize_memory_leak(target_mb: int) -> None:
    """Grow or shrink the retained buffer toward `target_mb`.

    Allocated in ~8 MiB chunks rather than one huge bytearray for two
    reasons: a single 2 GiB allocation is far more likely to fail outright
    on a memory-constrained container (giving a MemoryError instead of the
    gradual RSS climb we want to demonstrate), and chunking is what makes
    shrinking possible at all — we can drop references one chunk at a time.
    """
    with _memory_lock:
        target_bytes = target_mb * 1024 * 1024
        current = sum(len(chunk) for chunk in _leaked_chunks)

        while current < target_bytes:
            chunk_bytes = min(_MEMORY_CHUNK_MB * 1024 * 1024, target_bytes - current)
            chunk = bytearray(chunk_bytes)
            # Touch one byte per page. bytearray() is zero-filled, but on
            # Linux a large zero allocation can be served by lazily mapped
            # pages that never count toward RSS until written — which would
            # leave process_resident_memory_bytes flat and make the fault
            # invisible. Writing a page-strided pattern forces the kernel to
            # actually back every page.
            for offset in range(0, chunk_bytes, _PAGE_SIZE):
                chunk[offset] = 0xA5
            _leaked_chunks.append(chunk)
            current += chunk_bytes

        while current > target_bytes and _leaked_chunks:
            # pop() drops the only reference to the chunk, so CPython's
            # refcounting frees it immediately — no gc pass required.
            current -= len(_leaked_chunks.pop())

        if target_bytes == 0:
            # Belt and braces for the reset path: clear() guarantees no
            # stragglers are still referenced by the list itself.
            _leaked_chunks.clear()


@dataclass(frozen=True)
class ChaosState:
    latency_ms: int = 0
    error_rate: float = 0.0
    db_failure: bool = False
    cpu_burn: bool = False
    # Approximate MiB retained in a module-level buffer. Bounded at 2048 in
    # the API model.
    #
    # IMPORTANT / deliberately not guarded against: k8s/base/citizen-service/
    # deployment.yaml sets a 256Mi memory limit, so any value that pushes
    # the pod's RSS past that limit will get the container OOMKilled and
    # restarted by the kubelet. That is not a bug in this fault — it is the
    # honest consequence of leaking more memory than the pod is allowed to
    # use, and it is one of the two legitimate ways to demo a memory
    # incident (the other being a sub-limit leak that alerts on rising RSS
    # without ever dying). Both are useful: OOMKill exercises
    # CrashLoop/restart-count alerting and pod-recovery behaviour, while a
    # sub-limit leak exercises the slow-burn detection path. The bound is
    # 2048 rather than 256 precisely so the OOMKill demo remains possible;
    # scripts/incident-scenarios.sh deliberately picks a sub-limit value so
    # its memory-leak scenario does not lose the pod mid-run.
    memory_leak_mb: int = 0


class ChaosController:
    def __init__(self) -> None:
        self._lock = RLock()
        self._state = ChaosState()
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
        cpu_burn: bool | None = None,
        memory_leak_mb: int | None = None,
    ) -> ChaosState:
        with self._lock:
            previous = self._state
            self._state = ChaosState(
                latency_ms=self._state.latency_ms if latency_ms is None else latency_ms,
                error_rate=self._state.error_rate if error_rate is None else error_rate,
                db_failure=self._state.db_failure if db_failure is None else db_failure,
                cpu_burn=self._state.cpu_burn if cpu_burn is None else cpu_burn,
                memory_leak_mb=(
                    self._state.memory_leak_mb
                    if memory_leak_mb is None
                    else memory_leak_mb
                ),
            )
            self._publish()
            new_state = replace(self._state)

        # Side effects are applied *outside* self._lock on purpose. Growing
        # the leak buffer by hundreds of MiB takes real wall-clock time, and
        # every request in the chaos middleware calls get(), which takes this
        # same lock — holding it across the allocation would stall /healthz
        # and /metrics, i.e. exactly the observability we need to stay alive
        # during the incident. Consequence, accepted knowingly: two
        # simultaneous conflicting POSTs could apply their side effects out
        # of order. Both operations are idempotent "converge to this target"
        # calls guarded by their own locks, so the worst case is that the
        # buffer settles on the losing writer's size until the next call —
        # acceptable for a single-operator chaos control plane.
        self._apply_side_effects(previous, new_state)
        return new_state

    def reset(self) -> ChaosState:
        with self._lock:
            previous = self._state
            self._state = ChaosState()
            self._publish()
            new_state = replace(self._state)
        # Reset must be a genuine remediation, not just a metrics change:
        # this stops the burner thread's busy phase and frees every leaked
        # chunk.
        self._apply_side_effects(previous, new_state)
        return new_state

    def record(self, fault_type: str) -> None:
        chaos_injections_total.labels(fault_type=fault_type).inc()

    def _apply_side_effects(self, previous: ChaosState, current: ChaosState) -> None:
        if current.cpu_burn != previous.cpu_burn:
            _cpu_burner.set(current.cpu_burn)
            # Counted on the False -> True transition only. Re-POSTing the
            # same "cpu_burn: true" is a no-op, not a second injection, so
            # counting it would make chaos_injections_total misleading.
            if current.cpu_burn:
                self.record("cpu_burn")

        if current.memory_leak_mb != previous.memory_leak_mb:
            _resize_memory_leak(current.memory_leak_mb)
            # Same reasoning: only a change to a non-zero target is a new
            # injection. Shrinking to 0 is remediation, not injection.
            if current.memory_leak_mb:
                self.record("memory_leak")

    def _publish(self) -> None:
        chaos_latency_ms.set(self._state.latency_ms)
        chaos_error_rate.set(self._state.error_rate)
        chaos_db_failure.set(1 if self._state.db_failure else 0)
        chaos_cpu_burn.set(1 if self._state.cpu_burn else 0)
        chaos_memory_leak_mb.set(self._state.memory_leak_mb)


controller = ChaosController()
