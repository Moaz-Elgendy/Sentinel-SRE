"""
Sentinel Administration & Tuning Center — Policy configuration bounds and
validation.

Kept separate from app/routers/config.py (which does I/O: reading the
request, writing the audit trail) the same way app/lifecycle/policy.py is
kept separate from the routers that call it — this module is pure
(no I/O, no store, no clock) so its safety bounds are exercised directly by
tests with no HTTP layer involved, which is the only way those bounds are
worth trusting.

### What is editable, and what is not

`EDITABLE_FIELDS` is the complete list of `PolicyConfig` fields the GUI can
change. Two real `PolicyConfig` fields are deliberately absent from it:
`denied_deployments` and `denied_namespaces` — the frozen deny-lists. Those
are sourced from `Settings.denied_deployments_frozen`/
`denied_namespaces_frozen`, which core/config.py's own docstring says is
"NOT env-configurable on purpose" — this module holds that line at the next
layer up. `validate_changes` rejects any submitted field outside
`EDITABLE_FIELDS` by name, and separately rejects an `allowed_*` change that
would merely *contain* something already on the frozen list (which
`PolicyEngine` would deny anyway, since the frozen check runs before the
allow-list check — see policy.py's "frozen deny-list beats the allow-list" —
but a clear rejection here is better than a config change that silently
does nothing).

### Why bounds live here and not only in the frontend

Per the explicit requirement: the frontend must never be trusted to enforce
safety. Every bound below is re-checked here, in the backend, on every
apply — a request that skipped the GUI entirely and called
`POST /api/config/policy/apply` directly gets exactly the same enforcement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lifecycle.policy import PolicyConfig

EDITABLE_FIELDS: dict[str, str] = {
    "allowed_namespaces": "str_list",
    "allowed_deployments": "str_list",
    "confidence_rollback": "float",
    "confidence_restart": "float",
    "confidence_scale": "float",
    "confidence_chaos_reset": "float",
    "min_replicas": "int",
    "max_replicas": "int",
    "max_actions_per_incident": "int",
    "action_cooldown_seconds": "int",
    "deployment_correlation_window_minutes": "int",
}

# Hard, non-negotiable bounds. Below CONFIDENCE_FLOOR, a "confidence
# threshold" stops meaning anything — 0.5 is the line between "Sentinel
# requires a genuine majority-confidence hypothesis" and "Sentinel will act
# on a coin flip". MIN_REPLICAS_FLOOR of 1 mirrors core/config.py's own
# comment on the setting verbatim: zero replicas is an outage, not a
# remediation.
CONFIDENCE_FLOOR = 0.5
CONFIDENCE_CEILING = 1.0
MIN_REPLICAS_FLOOR = 1
MAX_REPLICAS_CEILING = 20
MAX_ACTIONS_PER_INCIDENT_BOUNDS = (1, 10)
ACTION_COOLDOWN_SECONDS_BOUNDS = (30, 3600)
CORRELATION_WINDOW_MINUTES_BOUNDS = (1, 1440)


def bounds_metadata() -> dict[str, Any]:
    """Informational only — tells the GUI what range to show in a slider or
    input hint. Never trusted for enforcement; see this module's docstring."""
    return {
        "confidence_rollback": {"min": CONFIDENCE_FLOOR, "max": CONFIDENCE_CEILING, "type": "float"},
        "confidence_restart": {"min": CONFIDENCE_FLOOR, "max": CONFIDENCE_CEILING, "type": "float"},
        "confidence_scale": {"min": CONFIDENCE_FLOOR, "max": CONFIDENCE_CEILING, "type": "float"},
        "confidence_chaos_reset": {"min": CONFIDENCE_FLOOR, "max": CONFIDENCE_CEILING, "type": "float"},
        "min_replicas": {"min": MIN_REPLICAS_FLOOR, "max": MAX_REPLICAS_CEILING, "type": "int"},
        "max_replicas": {"min": MIN_REPLICAS_FLOOR, "max": MAX_REPLICAS_CEILING, "type": "int"},
        "max_actions_per_incident": {
            "min": MAX_ACTIONS_PER_INCIDENT_BOUNDS[0],
            "max": MAX_ACTIONS_PER_INCIDENT_BOUNDS[1],
            "type": "int",
        },
        "action_cooldown_seconds": {
            "min": ACTION_COOLDOWN_SECONDS_BOUNDS[0],
            "max": ACTION_COOLDOWN_SECONDS_BOUNDS[1],
            "type": "int",
        },
        "deployment_correlation_window_minutes": {
            "min": CORRELATION_WINDOW_MINUTES_BOUNDS[0],
            "max": CORRELATION_WINDOW_MINUTES_BOUNDS[1],
            "type": "int",
        },
        "allowed_namespaces": {"type": "str_list"},
        "allowed_deployments": {"type": "str_list"},
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


def current_value(config: PolicyConfig, field: str) -> Any:
    value = getattr(config, field)
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def snapshot(config: PolicyConfig) -> dict[str, Any]:
    """Every editable field's current value — the GET response body and
    the base a diff is computed against."""
    return {field: current_value(config, field) for field in EDITABLE_FIELDS}


def validate_changes(
    config: PolicyConfig,
    changes: dict[str, Any],
    frozen_deployments: frozenset[str],
    frozen_namespaces: frozenset[str],
) -> tuple[list[str], list[FieldDiff]]:
    """Returns (errors, diffs). If `errors` is non-empty, `diffs` must be
    discarded and nothing applied — the caller (routers/config.py) enforces
    this, but this function itself never mutates anything either way,
    making it safe to call purely to preview."""
    errors: list[str] = []
    diffs: list[FieldDiff] = []

    unknown = set(changes) - set(EDITABLE_FIELDS)
    for field in sorted(unknown):
        errors.append(
            f"'{field}' is not a configurable policy value. If this is a frozen deny-list "
            "field, it is intentionally not configurable from anywhere — see "
            "core/config.py's denied_deployments_frozen/denied_namespaces_frozen."
        )

    # min/max replicas constrain each other, so resolve both proposed values
    # up front regardless of which one (or both, or neither) is in `changes`.
    proposed_min = changes.get("min_replicas", config.min_replicas)
    proposed_max = changes.get("max_replicas", config.max_replicas)

    for field, kind in EDITABLE_FIELDS.items():
        if field not in changes:
            continue
        new_value = changes[field]
        old_value = current_value(config, field)

        if kind == "float":
            if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
                errors.append(f"{field} must be a number.")
                continue
            new_value = float(new_value)
            if not (CONFIDENCE_FLOOR <= new_value <= CONFIDENCE_CEILING):
                errors.append(
                    f"{field} must be between {CONFIDENCE_FLOOR} and {CONFIDENCE_CEILING} "
                    f"(got {new_value}). Below {CONFIDENCE_FLOOR}, this stops being a tuning "
                    "choice and starts being 'act on a guess' — not a range this system offers."
                )
                continue
            warning = None
            if new_value < old_value:
                action_label = field.removeprefix("confidence_")
                warning = (
                    f"You are LOWERING the autonomous {action_label} confidence threshold "
                    f"from {old_value:.2f} to {new_value:.2f}. Sentinel will act autonomously "
                    "with less certainty than before."
                )
            diffs.append(FieldDiff(field, old_value, new_value, warning))

        elif kind == "int":
            if isinstance(new_value, bool) or not isinstance(new_value, int):
                errors.append(f"{field} must be a whole number.")
                continue

            if field == "min_replicas":
                if new_value < MIN_REPLICAS_FLOOR:
                    errors.append(
                        f"min_replicas can never go below {MIN_REPLICAS_FLOOR} — zero replicas "
                        "is an outage, not a remediation setting. This floor is enforced in the "
                        "backend and is not adjustable from the GUI, by design."
                    )
                    continue
                if new_value > proposed_max:
                    errors.append(
                        f"min_replicas ({new_value}) cannot exceed max_replicas ({proposed_max})."
                    )
                    continue
                diffs.append(FieldDiff(field, old_value, new_value))
                continue

            if field == "max_replicas":
                if new_value > MAX_REPLICAS_CEILING:
                    errors.append(f"max_replicas cannot exceed {MAX_REPLICAS_CEILING}.")
                    continue
                if new_value < proposed_min:
                    errors.append(
                        f"max_replicas ({new_value}) cannot be less than min_replicas "
                        f"({proposed_min})."
                    )
                    continue
                diffs.append(FieldDiff(field, old_value, new_value))
                continue

            if field == "max_actions_per_incident":
                lo, hi = MAX_ACTIONS_PER_INCIDENT_BOUNDS
                if not (lo <= new_value <= hi):
                    errors.append(f"max_actions_per_incident must be between {lo} and {hi}.")
                    continue
                warning = (
                    "Raising the per-incident action cap lets Sentinel try more things "
                    "autonomously before it escalates."
                    if new_value > old_value
                    else None
                )
                diffs.append(FieldDiff(field, old_value, new_value, warning))
                continue

            if field == "action_cooldown_seconds":
                lo, hi = ACTION_COOLDOWN_SECONDS_BOUNDS
                if not (lo <= new_value <= hi):
                    errors.append(f"action_cooldown_seconds must be between {lo} and {hi}.")
                    continue
                warning = (
                    "Shortening the cooldown lets Sentinel retry an action against the same "
                    "target sooner after a previous one."
                    if new_value < old_value
                    else None
                )
                diffs.append(FieldDiff(field, old_value, new_value, warning))
                continue

            if field == "deployment_correlation_window_minutes":
                lo, hi = CORRELATION_WINDOW_MINUTES_BOUNDS
                if not (lo <= new_value <= hi):
                    errors.append(
                        f"deployment_correlation_window_minutes must be between {lo} and {hi}."
                    )
                    continue
                warning = (
                    "Widening the correlation window makes rollback's deploy-correlation "
                    "check accept older deployments as 'recent enough'."
                    if new_value > old_value
                    else None
                )
                diffs.append(FieldDiff(field, old_value, new_value, warning))
                continue

        elif kind == "str_list":
            if not isinstance(new_value, list) or not all(isinstance(x, str) for x in new_value):
                errors.append(f"{field} must be a list of strings.")
                continue
            cleaned = sorted({x.strip() for x in new_value if x.strip()})
            frozen = frozen_deployments if field == "allowed_deployments" else frozen_namespaces
            collisions = sorted(frozen.intersection(cleaned))
            if collisions:
                errors.append(
                    f"{field} cannot include {collisions} — these are permanently deny-listed "
                    "and not configurable from the GUI or anywhere else (see core/config.py's "
                    "denied_deployments_frozen/denied_namespaces_frozen)."
                )
                continue
            added = sorted(set(cleaned) - set(old_value))
            warning = (
                f"Adding {added} means Sentinel can now autonomously act on it."
                if added
                else None
            )
            diffs.append(FieldDiff(field, old_value, cleaned, warning))

    return errors, diffs


def apply_diffs(config: PolicyConfig, diffs: list[FieldDiff]) -> None:
    """Mutate the LIVE `PolicyConfig` in place.

    `PolicyEngine.evaluate()` reads `self.config.<field>` fresh on every
    call (see policy.py's own design-principles docstring: "pure and
    synchronous... every input is an argument" — the config object itself is
    the one piece of engine state that lives across calls), so this takes
    effect starting with the very next incident evaluation. No restart, no
    cache to invalidate, nothing else to notify.

    Must only be called with diffs that already passed `validate_changes`
    with zero errors — this function does not re-validate, by design, so
    that `validate_changes` stays the single place bounds are enforced.
    """
    for diff in diffs:
        value = diff.new_value
        if isinstance(value, list):
            value = frozenset(value)
        setattr(config, diff.field, value)


def reload_stored_overrides(
    config: PolicyConfig,
    stored_overrides: dict[str, Any],
    frozen_deployments: frozenset[str],
    frozen_namespaces: frozenset[str],
) -> list[str]:
    """Re-apply previously-saved overrides (from `config_overrides` — see
    app/store/sqlite_store.py) onto a freshly-built `PolicyConfig`, through
    the SAME validated path a live `POST /api/config/policy/apply` uses —
    never a raw, unchecked assignment.

    Called once at startup (see main.py's lifespan) so a change an admin
    made before a restart is still in effect after one. Returns the list of
    validation errors, if any — non-empty means a previously-valid stored
    override no longer satisfies today's bounds (e.g. a Sentinel upgrade
    tightened one), which is loud-fail-worthy, not something to silently
    ignore, since it is a safety-relevant piece of state.
    """
    if not stored_overrides:
        return []
    errors, diffs = validate_changes(config, stored_overrides, frozen_deployments, frozen_namespaces)
    if not errors:
        apply_diffs(config, diffs)
    return errors
