"""
OpenAIReasoner — the original rca.py OpenAI call, moved here unchanged.

`base_url` empty (default) talks to real OpenAI. Set it to point the same
client at any OpenAI-API-compatible provider (Groq, OpenRouter, etc.) — see
Settings.openai_base_url's docstring for the constraint that matters (JSON
mode support on the chosen model). This is a straight extraction, not a
rewrite: same client construction, same call, same temperature/response
format/max_tokens, same broad except.
"""
from __future__ import annotations

import logging

from app.reasoning.base import Reasoner

logger = logging.getLogger(__name__)


class OpenAIReasoner(Reasoner):
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        base_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url
        self.label = f"openai:{model}"

    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError as exc:
            logger.warning("openai_package_unavailable", extra={"error_detail": str(exc)[:120]})
            return None

        try:
            client = AsyncOpenAI(
                api_key=self.api_key, timeout=self.timeout, base_url=(self.base_url or None)
            )
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                # Deterministic-ish. We are asking for an explanation of
                # fixed evidence, not creative writing.
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=600,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - openai raises many types
            logger.warning("openai_call_failed", extra={"error_detail": str(exc)[:200]})
            return None
