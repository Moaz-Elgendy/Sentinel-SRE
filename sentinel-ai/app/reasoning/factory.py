"""
Reasoner factory. The one place `Settings.llm_provider` gets read to decide
which `Reasoner` implementation to construct. build_context() (see
lifecycle/orchestrator.py) calls this once per environment; nothing else in
the lifecycle ever imports openai/gemini directly or checks the provider
string.

Returns None when the selected provider has no key configured — the same
"rule-based only" degraded mode as before this refactor. See
rca.enrich_with_llm for how a None reasoner is handled (identically to how a
missing OPENAI_API_KEY was handled previously).
"""
from __future__ import annotations

from typing import Any

from app.reasoning.base import Reasoner


def build_reasoner(settings_obj: Any) -> Reasoner | None:
    s = settings_obj
    if s.llm_provider == "gemini":
        if not s.gemini_api_key.strip():
            return None
        from app.reasoning.gemini_reasoner import GeminiReasoner  # noqa: PLC0415

        return GeminiReasoner(
            api_key=s.gemini_api_key,
            model=s.gemini_model,
            timeout=s.gemini_timeout_seconds,
        )

    # default: openai (also covers Groq/OpenRouter/etc via openai_base_url)
    if not s.openai_api_key.strip():
        return None
    from app.reasoning.openai_reasoner import OpenAIReasoner  # noqa: PLC0415

    return OpenAIReasoner(
        api_key=s.openai_api_key,
        model=s.openai_model,
        timeout=s.openai_timeout_seconds,
        base_url=s.openai_base_url,
    )
