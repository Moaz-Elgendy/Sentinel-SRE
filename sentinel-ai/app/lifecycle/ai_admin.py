"""
Sentinel Administration & Tuning Center — AI / Reasoning configuration.

### What this covers, and — just as important — what it deliberately does not

Before writing any of this, `app/reasoning/factory.py` and the two concrete
`Reasoner` implementations (`openai_reasoner.py`, `gemini_reasoner.py`) were
inspected to see what is genuinely configurable versus what only looks like
it should be. The answer:

* `llm_provider`, `openai_model`, `openai_timeout_seconds`, `openai_base_url`,
  `gemini_model`, and `gemini_timeout_seconds` are plain `Settings` fields
  read once, at `build_reasoner()` time, to construct whichever concrete
  `Reasoner` is active. These ARE genuinely configurable and live here.
* `openai_api_key` and `gemini_api_key` are NEVER exposed by this module —
  not in `EDITABLE_FIELDS`, not in `snapshot()`, not in `read_only_summary()`.
  This is structural, not a display-layer omission: a field simply not being
  in `EDITABLE_FIELDS` means `validate_changes` rejects any attempt to set
  it and `snapshot`/`read_only_summary` have no code path that could ever
  read one back. `read_only_summary()` does surface a `*_configured: bool`
  per provider (key present or not) — enough for an admin to see "will this
  provider actually work" without ever seeing key material.
* `temperature` is hardcoded to `0.0` in both `OpenAIReasoner` and
  `GeminiReasoner` (not a `Settings` field at all) — surfaced here as
  read-only for transparency, exactly like `rca_admin.py` surfaces the
  hardcoded confidence constants it does not own.

### The live-reconstruction problem this module solves

Unlike every other category so far, the thing that needs to change live is
not a mutable field on a long-lived engine object — it is WHICH concrete
`Reasoner` instance `ctx.reasoner` points at. `build_context()`
(orchestrator.py) builds `ctx.reasoner` once via `build_reasoner(settings)`
at startup; nothing about a plain settings mutation would rebuild it.
`rebuild_reasoner()` below is this category's `after_apply` hook (see
policy_admin.sync_dependent_engines for the precedent): after
`apply_diffs` mutates `ctx.settings` in place, it calls the SAME
`build_reasoner()` factory startup uses and reassigns `ctx.reasoner`.
`Orchestrator._run` reads `self.ctx.reasoner` fresh on every incident (see
orchestrator.py — it is never cached locally), so this takes effect
starting with the very next incident, no restart needed — the same
guarantee every other category in this file documents for itself.

`reload_stored_overrides` here therefore takes `ctx`, not a live object
directly — it needs `ctx.settings` to validate/apply against AND `ctx` itself
to call `rebuild_reasoner`. This mirrors `policy_admin.reload_stored_overrides`
(which also needs `ctx`, for the same "more than one thing must be kept in
sync" reason) rather than `rca_admin`/`remediation_admin`'s simpler
live-object-only signature.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EDITABLE_FIELDS: dict[str, str] = {
    "llm_provider": "enum",
    "openai_model": "string",
    "openai_timeout_seconds": "float",
    "openai_base_url": "string",
    "gemini_model": "string",
    "gemini_timeout_seconds": "float",
}

PROVIDER_CHOICES: tuple[str, ...] = ("openai", "gemini")

# Same "backend is authoritative" bounds pattern as rca_admin.BOUNDS — a
# timeout of 0 would mean every call fails instantly; anything past two
# minutes just makes a stuck incident take two minutes longer to notice.
TIMEOUT_BOUNDS: dict[str, tuple[float, float]] = {
    "openai_timeout_seconds": (1.0, 120.0),
    "gemini_timeout_seconds": (1.0, 120.0),
}


def bounds_metadata() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for field, kind in EDITABLE_FIELDS.items():
        if kind == "enum":
            meta[field] = {"type": kind, "choices": list(PROVIDER_CHOICES)}
        elif field in TIMEOUT_BOUNDS:
            lo, hi = TIMEOUT_BOUNDS[field]
            meta[field] = {"type": kind, "min": lo, "max": hi}
        else:
            meta[field] = {"type": kind}
    return meta


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


def snapshot(settings: Any) -> dict[str, Any]:
    return {field: getattr(settings, field) for field in EDITABLE_FIELDS}


def read_only_summary(settings: Any) -> dict[str, Any]:
    return {
        "description": (
            "Provider, model, timeout, and base URL for Sentinel's optional LLM-assisted "
            "root cause analysis (see RCA & Diagnosis for the rule-based detection this "
            "augments — the LLM can only narrow a hypothesis the rule engine already "
            "produced, per rca.apply_llm_response's confidence-delta cap, never invent one "
            "unsupported by evidence). API keys are never readable or writable from this "
            "page or any API response — set them via server environment variables only."
        ),
        "temperature": 0.0,
        "temperature_note": "Hardcoded to 0.0 in every Reasoner implementation, not configurable.",
        "openai_api_key_configured": bool(settings.openai_api_key.strip()),
        "gemini_api_key_configured": bool(settings.gemini_api_key.strip()),
    }


def _validate_provider(settings: Any, new_value: Any) -> tuple[str | None, str | None]:
    """Returns (error, warning) — at most one of which is non-None."""
    if not isinstance(new_value, str) or new_value not in PROVIDER_CHOICES:
        return f"llm_provider must be one of {list(PROVIDER_CHOICES)}.", None
    key_field = "gemini_api_key" if new_value == "gemini" else "openai_api_key"
    if not getattr(settings, key_field).strip():
        return None, (
            f"{key_field.upper()} is not set. Sentinel will fall back to rule-based-only "
            "RCA (llm_used=false on every incident) until a key is configured server-side "
            "— this API never accepts or displays keys."
        )
    return None, None


def validate_changes(settings: Any, changes: dict[str, Any]) -> tuple[list[str], list[FieldDiff]]:
    errors: list[str] = []
    diffs: list[FieldDiff] = []

    unknown = set(changes) - set(EDITABLE_FIELDS)
    for field in sorted(unknown):
        if field in ("openai_api_key", "gemini_api_key"):
            errors.append(
                f"'{field}' can never be read or written through this API. Set it via a "
                "server-side environment variable and restart Sentinel."
            )
        else:
            errors.append(f"'{field}' is not a configurable AI/reasoning value.")

    if "llm_provider" in changes:
        new_value = changes["llm_provider"]
        old_value = settings.llm_provider
        error, warning = _validate_provider(settings, new_value)
        if error:
            errors.append(error)
        else:
            diffs.append(FieldDiff("llm_provider", old_value, new_value, warning))

    for field in ("openai_model", "gemini_model", "openai_base_url"):
        if field in changes:
            new_value = changes[field]
            old_value = getattr(settings, field)
            if not isinstance(new_value, str):
                errors.append(f"'{field}' must be a string.")
            elif field != "openai_base_url" and not new_value.strip():
                errors.append(f"'{field}' cannot be empty.")
            else:
                diffs.append(FieldDiff(field, old_value, new_value))

    for field in ("openai_timeout_seconds", "gemini_timeout_seconds"):
        if field in changes:
            new_value = changes[field]
            old_value = getattr(settings, field)
            lo, hi = TIMEOUT_BOUNDS[field]
            if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
                errors.append(f"'{field}' must be a number.")
            elif not (lo <= float(new_value) <= hi):
                errors.append(f"'{field}' must be between {lo} and {hi} seconds.")
            else:
                diffs.append(FieldDiff(field, old_value, float(new_value)))

    return errors, diffs


def apply_diffs(settings: Any, diffs: list[FieldDiff]) -> None:
    for diff in diffs:
        setattr(settings, diff.field, diff.new_value)


def rebuild_reasoner(ctx: Any, diffs: list[FieldDiff]) -> None:
    """`after_apply` hook (see app/routers/config.py's `CategoryHandler`).
    Reconstructs `ctx.reasoner` from the just-mutated `ctx.settings` via the
    SAME `build_reasoner()` factory startup uses, so a provider/model/
    timeout/base_url change is live on the very next incident's RCA step —
    see this module's docstring for why a plain settings mutation alone
    would not be enough.
    """
    from app.reasoning.factory import build_reasoner  # noqa: PLC0415 - avoid import cycle at module load

    ctx.reasoner = build_reasoner(ctx.settings)


def reload_stored_overrides(ctx: Any, stored_overrides: dict[str, Any]) -> list[str]:
    """Called once at startup (see main.py's lifespan), same as
    policy_admin's — needs `ctx`, not just `ctx.settings`, because applying
    a stored override must also rebuild `ctx.reasoner` before Sentinel
    processes its first incident.
    """
    if not stored_overrides:
        return []
    errors, diffs = validate_changes(ctx.settings, stored_overrides)
    if not errors:
        apply_diffs(ctx.settings, diffs)
        rebuild_reasoner(ctx, diffs)
    return errors
