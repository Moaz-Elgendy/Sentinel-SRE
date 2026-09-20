"""
Reasoner factory. The one place `Settings.llm_provider` gets read to decide
which `Reasoner` implementation to construct. build_context() (see
lifecycle/orchestrator.py) calls this once per environment; nothing else in
the lifecycle ever imports openai/gemini/groq directly or checks the provider
string.

    Reasoner
    ├── OpenAIReasoner   (LLM_PROVIDER=openai; any OpenAI-compatible endpoint)
    ├── GroqReasoner     (LLM_PROVIDER=groq)
    ├── GeminiReasoner   (LLM_PROVIDER=gemini)
    └── future LocalReasoner

Returns None when the selected provider has no key configured — the same
"rule-based only" degraded mode as before this refactor. See
rca.enrich_with_llm for how a None reasoner is handled (identically to how a
missing OPENAI_API_KEY was handled previously).

Every Reasoner returned here gets a `ReasonerHealth` configured from Settings
(reasoning/health.py) so a failing provider is contained instead of retried
on every incident.
"""
from __future__ import annotations

import logging
from typing import Any

from app.reasoning.base import Reasoner
from app.reasoning.health import ReasonerHealth

logger = logging.getLogger(__name__)


def build_reasoner(settings_obj: Any) -> Reasoner | None:
    s = settings_obj
    reasoner: Reasoner | None

    if s.llm_provider == "gemini":
        if not s.gemini_api_key.strip():
            logger.warning(
                "reasoner_not_configured",
                extra={"provider": "gemini", "reason": "GEMINI_API_KEY not set"},
            )
            return None
        from app.reasoning.gemini_reasoner import (  # noqa: PLC0415
            GEMINI_API_BASE,
            GeminiReasoner,
        )

        reasoner = GeminiReasoner(
            api_key=s.gemini_api_key,
            model=s.gemini_model,
            timeout=s.gemini_timeout_seconds,
        )
        logger.info(
            "reasoner_configured",
            extra={"provider": "gemini", "model": s.gemini_model, "base_url": GEMINI_API_BASE},
        )
    elif s.llm_provider == "groq":
        if not s.groq_api_key.strip():
            logger.warning(
                "reasoner_not_configured",
                extra={"provider": "groq", "reason": "GROQ_API_KEY not set"},
            )
            return None
        from app.reasoning.groq_reasoner import GROQ_BASE_URL, GroqReasoner  # noqa: PLC0415

        reasoner = GroqReasoner(
            api_key=s.groq_api_key,
            model=s.groq_model,
            timeout=s.groq_timeout_seconds,
        )
        logger.info(
            "reasoner_configured",
            extra={"provider": "groq", "model": s.groq_model, "base_url": GROQ_BASE_URL},
        )
    else:
        # default: openai (also covers OpenRouter/etc via openai_base_url)
        if not s.openai_api_key.strip():
            logger.warning(
                "reasoner_not_configured",
                extra={"provider": "openai", "reason": "OPENAI_API_KEY not set"},
            )
            return None
        from app.reasoning.openai_reasoner import OpenAIReasoner  # noqa: PLC0415

        reasoner = OpenAIReasoner(
            api_key=s.openai_api_key,
            model=s.openai_model,
            timeout=s.openai_timeout_seconds,
            base_url=s.openai_base_url,
        )
        logger.info(
            "reasoner_configured",
            extra={
                "provider": "openai",
                "model": s.openai_model,
                "base_url": s.openai_base_url or "https://api.openai.com/v1",
            },
        )

    reasoner.health = ReasonerHealth(
        failure_threshold=getattr(s, "reasoner_failure_threshold", 3),
        cooldown_seconds=getattr(s, "reasoner_cooldown_seconds", 120),
    )
    return reasoner
