"""
Sentinel Administration & Tuning Center — RCA / Diagnosis configuration.

### What this covers, and — just as important — what it deliberately does not

Before writing any of this, the actual RCA implementation
(lifecycle/rca.py) was inspected to see what is genuinely configurable
versus what only looks like it should be. The answer: `rca.analyse()` takes
no threshold parameters at all. Every detection rule is hardcoded Python —
alertname/finding pattern matching, most-specific-first, with a fixed
confidence value baked into each rule (e.g. `0.97` for an active chaos
fault). `LLM_CONFIDENCE_DELTA_CAP`, `LLM_CONFIDENCE_CEILING`, and
`RULE_CONFIDENCE_MAX` are module-level constants, not settings. None of
that is exposed here as an editable value, and none of it is faked as one
— see `read_only_summary()` below, which surfaces it for VIEWING ("what
rules are controlling its autonomy") without pretending it can be tuned.

What genuinely IS configurable, and lives here: the evidence/validation
thresholds that decide what counts as "bad enough to correlate with an
incident" and, later, "recovered" — `ValidationThresholds`
(lifecycle/validation.py), held live at `ctx.validator.thresholds`. This
is the same object correlation.correlate() and RecoveryValidator.validate()
both read from AT CALL TIME (see the Administration & Tuning Center's fix
to orchestrator.py, which made this the single source of truth instead of
a second copy read from `settings`) — so, exactly like `PolicyConfig` in
policy_admin.py, mutating this live object takes effect starting with the
very next incident.

`deployment_correlation_window_minutes` is deliberately NOT duplicated
here even though the original request lists "correlation windows" under
RCA — it is a `PolicyConfig` field (see policy_admin.py) and is edited
there; `read_only_summary()` surfaces its current value here for
visibility without a second editable control for the same backend field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lifecycle import rca
from app.lifecycle.validation import ValidationThresholds

EDITABLE_FIELDS: dict[str, str] = {
    "max_error_rate": "float",
    "max_p95_seconds": "float",
    "max_cpu_cores": "float",
    "max_memory_bytes": "float",
    "settle_seconds": "int",
    "timeout_seconds": "int",
    "poll_interval_seconds": "int",
}

# Hard, non-negotiable bounds — same "backend is authoritative" rule as
# policy_admin.py's CONFIDENCE_FLOOR etc.
BOUNDS: dict[str, tuple[float, float]] = {
    "max_error_rate": (0.0, 1.0),
    "max_p95_seconds": (0.1, 60.0),
    "max_cpu_cores": (0.1, 16.0),
    "max_memory_bytes": (50_000_000, 8_000_000_000),
    "settle_seconds": (0, 300),
    "timeout_seconds": (10, 600),
    "poll_interval_seconds": (1, 120),
}


def bounds_metadata() -> dict[str, Any]:
    return {
        field: {"min": lo, "max": hi, "type": EDITABLE_FIELDS[field]} for field, (lo, hi) in BOUNDS.items()
    }


@dataclass
class FieldDiff:
    field: str
    old_value: Any
    new_value: Any
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "warning": self.warning,
        }


def snapshot(thresholds: ValidationThresholds) -> dict[str, Any]:
    return {field: getattr(thresholds, field) for field in EDITABLE_FIELDS}


def read_only_summary(policy_config: Any) -> dict[str, Any]:
    """What an SRE administrator sees but cannot edit here — the actual
    detection logic and its safety constants, plus a cross-reference to the
    one RCA-adjacent value that IS editable, just not from this page."""
    return {
        "root_causes": [rc.value for rc in rca.RootCause],
        "rule_based_detection": {
            "description": (
                "Root cause detection is entirely rule-based: hardcoded pattern matching "
                "against alert labels and correlated evidence, most-specific-first (an active "
                "chaos fault is checked before every symptom-based rule, since it is direct "
                "evidence of cause rather than a symptom). Each rule carries its own fixed "
                "confidence value. None of this is data-driven, so none of it is editable from "
                "this page — changing it would mean changing the reasoning code itself, not a "
                "configuration value."
            ),
            "llm_confidence_delta_cap": rca.LLM_CONFIDENCE_DELTA_CAP,
            "llm_confidence_ceiling": rca.LLM_CONFIDENCE_CEILING,
            "rule_confidence_max": rca.RULE_CONFIDENCE_MAX,
        },
        "deployment_correlation_window_minutes": {
            "value": policy_config.deployment_correlation_window_minutes,
            "edit_via": "/api/config/policy",
        },
    }


def validate_changes(
    thresholds: ValidationThresholds, changes: dict[str, Any]
) -> tuple[list[str], list[FieldDiff]]:
    errors: list[str] = []
    diffs: list[FieldDiff] = []

    unknown = set(changes) - set(EDITABLE_FIELDS)
    for field in sorted(unknown):
        errors.append(
            f"'{field}' is not a configurable RCA/validation value. Detection rules and their "
            "confidence values are hardcoded and not configurable from anywhere — see "
            "lifecycle/rca.py."
        )

    proposed_timeout = changes.get("timeout_seconds", thresholds.timeout_seconds)
    proposed_settle = changes.get("settle_seconds", thresholds.settle_seconds)
    proposed_poll = changes.get("poll_interval_seconds", thresholds.poll_interval_seconds)

    for field, kind in EDITABLE_FIELDS.items():
        if field not in changes:
            continue
        new_value = changes[field]
        old_value = getattr(thresholds, field)

        is_bool = isinstance(new_value, bool)
        is_number = isinstance(new_value, (int, float))
        if is_bool or not is_number:
            errors.append(f"{field} must be a number.")
            continue

        lo, hi = BOUNDS[field]
        if kind == "int":
            if not float(new_value).is_integer():
                errors.append(f"{field} must be a whole number.")
                continue
            new_value = int(new_value)
        else:
            new_value = float(new_value)

        if not (lo <= new_value <= hi):
            errors.append(f"{field} must be between {lo} and {hi} (got {new_value}).")
            continue

        if field == "timeout_seconds" and new_value <= proposed_settle:
            errors.append(
                f"timeout_seconds ({new_value}) must be greater than settle_seconds "
                f"({proposed_settle}) — otherwise validation would time out before it ever polls."
            )
            continue
        if field == "timeout_seconds" and new_value <= proposed_poll:
            errors.append(
                f"timeout_seconds ({new_value}) must be greater than poll_interval_seconds "
                f"({proposed_poll})."
            )
            continue
        if field in ("settle_seconds", "poll_interval_seconds") and proposed_timeout <= new_value:
            errors.append(
                f"{field} ({new_value}) must be less than timeout_seconds ({proposed_timeout})."
            )
            continue

        warning = None
        if field == "max_error_rate" and new_value > old_value:
            warning = "Raising the max error rate makes both correlation and recovery validation more tolerant of errors — Sentinel will treat a noisier service as 'recovered' sooner."
        elif field in ("max_p95_seconds", "max_cpu_cores", "max_memory_bytes") and new_value > old_value:
            warning = f"Raising {field} makes Sentinel more tolerant here — it will correlate and validate against a higher bar for 'bad'."
        elif field == "timeout_seconds" and new_value < old_value:
            warning = "Shortening the validation timeout gives a slow-to-recover service less time before Sentinel calls the action failed."

        diffs.append(FieldDiff(field, old_value, new_value, warning))

    return errors, diffs


def apply_diffs(thresholds: ValidationThresholds, diffs: list[FieldDiff]) -> None:
    """Mutate the LIVE `ValidationThresholds` in place — see this module's
    docstring for why that is enough to take effect immediately, with no
    restart, exactly like `policy_admin.apply_diffs`."""
    for diff in diffs:
        setattr(thresholds, diff.field, diff.new_value)


def reload_stored_overrides(thresholds: ValidationThresholds, stored_overrides: dict[str, Any]) -> list[str]:
    if not stored_overrides:
        return []
    errors, diffs = validate_changes(thresholds, stored_overrides)
    if not errors:
        apply_diffs(thresholds, diffs)
    return errors
