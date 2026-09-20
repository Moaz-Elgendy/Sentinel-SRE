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

### Diagnosability (why a Gemini HTTP 404 is now self-explaining)

The request is:

    POST https://generativelanguage.googleapis.com/v1beta/models/<GEMINI_MODEL>:generateContent
    header  x-goog-api-key: <key>

On a non-200 this logs ONE structured `gemini_call_http_error` line with the
endpoint (no key), model, API version, HTTP status, Google's own error
`status`/`message`, the client library, and a hint. A 404 from this endpoint
means Google could not find `<GEMINI_MODEL>` for that API version/method (a
retired, renamed or mistyped model — the response body says which), NOT that
the key is wrong (a bad key is 400/403). On the first 404 per model it also
calls ListModels once and logs the models that DO support generateContent, so
the fix is readable straight from the logs.

The API key is sent in a header rather than the `?key=` query string so it
can never end up in a URL that an exception message, proxy log or traceback
echoes; every logged string is additionally scrubbed of the key.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.reasoning.base import Reasoner

logger = logging.getLogger(__name__)

GEMINI_HOST = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
GEMINI_API_BASE = f"{GEMINI_HOST}/{GEMINI_API_VERSION}/models"


class GeminiReasoner(Reasoner):
    def __init__(self, api_key: str, model: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.label = f"gemini:{model}"
        # Models already diagnosed via ListModels (once per model per process
        # — a dead model must not add a second HTTP call to every incident).
        self._diagnosed: set[str] = set()

    # -- helpers ----------------------------------------------------------
    def _endpoint(self) -> str:
        return f"{GEMINI_API_BASE}/{self.model}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _scrub(self, text: str, limit: int = 600) -> str:
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return text[:limit]

    @staticmethod
    def _parse_error(resp: httpx.Response) -> tuple[str | None, str | None]:
        """(google error status, google error message) if the body is Google's
        standard error envelope; (None, None) otherwise."""
        try:
            err = (resp.json() or {}).get("error") or {}
            return err.get("status"), err.get("message")
        except (ValueError, AttributeError):
            return None, None

    async def _log_available_models(self, client: httpx.AsyncClient) -> None:
        """One-shot ListModels so a 404 names the models that would work."""
        if self.model in self._diagnosed:
            return
        self._diagnosed.add(self.model)
        try:
            resp = await client.get(
                GEMINI_API_BASE, headers=self._headers(), params={"pageSize": 200}
            )
            if resp.status_code != 200:
                logger.warning(
                    "gemini_list_models_failed",
                    extra={"status_code": resp.status_code, "body": self._scrub(resp.text, 300)},
                )
                return
            names = sorted(
                str(m.get("name", "")).removeprefix("models/")
                for m in (resp.json().get("models") or [])
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
            )
            logger.warning(
                "gemini_available_models",
                extra={
                    "configured_model": self.model,
                    "configured_model_listed": self.model in names,
                    "generate_content_models": names[:40],
                    "detail": "Set GEMINI_MODEL (or the AI config page) to one of these.",
                },
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("gemini_list_models_failed", extra={"error_detail": self._scrub(str(exc), 200)})

    # -- Reasoner ---------------------------------------------------------
    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        url = self._endpoint()
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
                resp = await client.post(url, headers=self._headers(), json=body)
                if resp.status_code != 200:
                    g_status, g_message = self._parse_error(resp)
                    hint = (
                        "HTTP 404 from generateContent means Google could not find this model "
                        f"for API version {GEMINI_API_VERSION} (retired, renamed or mistyped) — "
                        "not a key problem (bad keys are 400/403). See gemini_available_models."
                        if resp.status_code == 404
                        else None
                    )
                    logger.warning(
                        "gemini_call_http_error",
                        extra={
                            "endpoint": url,
                            "model": self.model,
                            "api_version": GEMINI_API_VERSION,
                            "http_method": "POST",
                            "status_code": resp.status_code,
                            "google_status": g_status,
                            "google_message": self._scrub(g_message or "", 300) or None,
                            "client_library": f"httpx/{httpx.__version__} (REST, no Google SDK)",
                            "request_id": resp.headers.get("x-goog-request-id"),
                            "body": self._scrub(resp.text),
                            "hint": hint,
                        },
                    )
                    self.report_failure(
                        f"HTTP {resp.status_code} {g_status or ''} from {url}: "
                        f"{self._scrub(g_message or resp.text, 200)}",
                        resp.status_code,
                    )
                    if resp.status_code == 404:
                        await self._log_available_models(client)
                    return None
            payload = resp.json()
            candidates = payload.get("candidates") or []
            if not candidates:
                logger.warning(
                    "gemini_call_no_candidates",
                    extra={"model": self.model, "payload": self._scrub(str(payload), 300)},
                )
                self.report_failure("Gemini returned no candidates (blocked or empty response)")
                return None
            parts = candidates[0].get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                self.report_failure("Gemini returned an empty candidate")
                return None
            self.report_success()
            return text
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            detail = self._scrub(f"{type(exc).__name__}: {exc}", 200)
            logger.warning(
                "gemini_call_failed",
                extra={"endpoint": url, "model": self.model, "error_detail": detail},
            )
            self.report_failure(detail)
            return None
