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
        self.provider_name = "openai"

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
            text = (response.choices[0].message.content or "").strip()
            if not text:
                self.report_failure("provider returned an empty completion")
                return None
            self.report_success()
            return text
        except Exception as exc:  # noqa: BLE001 - openai raises many types
            # `status_code` exists on openai.APIStatusError (4xx/5xx) and is
            # absent on connection/timeout errors — both are logged the same
            # way, with the endpoint and model that were actually used, so a
            # provider problem is diagnosable from this one line.
            status_code = getattr(exc, "status_code", None)
            detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(
                "openai_call_failed",
                extra={
                    "provider": self.provider_name,
                    "endpoint": self.base_url or "https://api.openai.com/v1",
                    "model": self.model,
                    "status_code": status_code,
                    "error_detail": detail.replace(self.api_key, "***") if self.api_key else detail,
                },
            )
            self.report_failure(
                detail.replace(self.api_key, "***") if self.api_key else detail, status_code
            )
            return None
