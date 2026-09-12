"""
GeminiReasoner — talks to the Gemini `generateContent` REST endpoint
directly over httpx, rather than adding the `google-generativeai` SDK as a
dependency. Sentinel already depends on httpx for everything else (Loki,
Prometheus, GitHub, Slack), so this keeps the dependency surface small; if
the SDK is ever wanted for streaming/function-calling later, swap this one
file, the `Reasoner` interface does not change.

JSON mode: Gemini's `responseMimeType: "application/json"` in
`generationConfig` is the equivalent of OpenAI's `response_format:
json_object` — the model is constrained to emit a JSON object, same
guarantee `apply_llm_response()` in rca.py relies on.
"""
from __future__ import annotations

import logging

import httpx

from app.reasoning.base import Reasoner

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiReasoner(Reasoner):
    def __init__(self, api_key: str, model: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.label = f"gemini:{model}"

    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 600,
                "responseMimeType": "application/json",
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    params={"key": self.api_key},
                    json=body,
                )
            if resp.status_code != 200:
                logger.warning(
                    "gemini_call_http_error",
                    extra={"status_code": resp.status_code, "body": resp.text[:300]},
                )
                return None
            payload = resp.json()
            candidates = payload.get("candidates") or []
            if not candidates:
                logger.warning("gemini_call_no_candidates", extra={"payload": str(payload)[:300]})
                return None
            parts = candidates[0].get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("gemini_call_failed", extra={"error_detail": str(exc)[:200]})
            return None
