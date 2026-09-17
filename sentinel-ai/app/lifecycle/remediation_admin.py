"""
Sentinel Administration & Tuning Center — Remediation configuration.

### What this covers, and — just as important — what it deliberately does not

`decision.py`'s `ACTION_LADDER` (root cause -> ordered candidate actions,
e.g. `BAD_DEPLOYMENT -> (rollback, restart)`) was inspected before writing
any of this. It is a hardcoded Python dict where the ORDER encodes real
safety reasoning per entry (see the inline comments in decision.py: a
rollback fallback to restart is safe because "the Policy Engine will
re-evaluate confidence for it"; `DATABASE_FAILURE` maps to an empty tuple
specifically because it is "never remediable"). There is no validation
layer that could check a proposed reordering against those invariants, and
turning it into admin-editable data would mean trusting an admin's
reordering to preserve safety properties that today are preserved by
review of the actual reasoning in the code. So `ACTION_LADDER` is exposed
here strictly READ-ONLY (`read_only_summary()`), for the "what rules are
controlling its autonomy" answer the console owes an SRE — not as a fake
setting.

What genuinely IS configurable: `RemediationEngine.dry_run` — whether
Sentinel's remediation step actually mutates the cluster or only logs what
it would have done (see lifecycle/remediation.py's `execute`). This
inspection found it was, like several other fields before it, a
construction-time snapshot with no fewer than SIX separate places reading
`settings.dry_run` directly for display purposes (main.py's startup log x2,
routers/dashboard.py, routers/health.py, and two timeline messages in
orchestrator.py) — none of which would have reflected a live toggle. All
were redirected to read `ctx.remediation.dry_run` (the one exception:
main.py's two STARTUP log lines, which correctly describe boot-time
configuration and cannot and should not reflect a change that, by
definition, hasn't happened yet at that point in the code).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lifecycle.decision import ACTION_LADDER, FALLBACK_DISCOUNT
from app.lifecycle.remediation import RemediationEngine

EDITABLE_FIELDS: dict[str, str] = {"dry_run": "bool"}


def bounds_metadata() -> dict[str, Any]:
    return {"dry_run": {"type": "bool"}}


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


def snapshot(remediation: RemediationEngine) -> dict[str, Any]:
    return {field: getattr(remediation, field) for field in EDITABLE_FIELDS}


def read_only_summary() -> dict[str, Any]:
    return {
        "description": (
            "Root cause -> candidate action mapping, in order. This encodes real safety "
            "reasoning per entry (e.g. why a rollback safely falls back to a restart, why "
            "a database failure maps to no autonomous action at all) and is not data-driven "
            "— it is not editable from this page or anywhere else. Sentinel tries the first "
            "action; if the Policy Engine denies it, it discounts confidence and tries the "
            "next, until the ladder for that root cause is exhausted."
        ),
        "action_ladder": {
            root_cause.value: [action.value for action in actions]
            for root_cause, actions in ACTION_LADDER.items()
        },
        "fallback_confidence_discount": FALLBACK_DISCOUNT,
    }


def validate_changes(
    remediation: RemediationEngine, changes: dict[str, Any]
) -> tuple[list[str], list[FieldDiff]]:
    errors: list[str] = []
    diffs: list[FieldDiff] = []

    unknown = set(changes) - set(EDITABLE_FIELDS)
    for field in sorted(unknown):
        errors.append(
            f"'{field}' is not a configurable remediation value. The root-cause-to-action "
            "mapping (the 'action ladder') is hardcoded and not configurable from anywhere "
            "— see lifecycle/decision.py."
        )

    if "dry_run" in changes:
        new_value = changes["dry_run"]
        old_value = remediation.dry_run
        if not isinstance(new_value, bool):
            errors.append("dry_run must be true or false.")
        else:
            warning = None
            if old_value is True and new_value is False:
                warning = (
                    "You are turning DRY RUN OFF. Sentinel will start actually mutating the "
                    "cluster (restarting/rolling back/scaling deployments, resetting chaos "
                    "faults) the next time it acts, instead of only logging what it would do."
                )
            diffs.append(FieldDiff("dry_run", old_value, new_value, warning))

    return errors, diffs


def apply_diffs(remediation: RemediationEngine, diffs: list[FieldDiff]) -> None:
    """Mutate the LIVE `RemediationEngine` in place — `execute()` reads
    `self.dry_run` fresh on every call, so this takes effect starting with
    the very next remediation attempt, no restart needed."""
    for diff in diffs:
        setattr(remediation, diff.field, diff.new_value)


def reload_stored_overrides(remediation: RemediationEngine, stored_overrides: dict[str, Any]) -> list[str]:
    if not stored_overrides:
        return []
    errors, diffs = validate_changes(remediation, stored_overrides)
    if not errors:
        apply_diffs(remediation, diffs)
    return errors
