#!/usr/bin/env python3
"""
Reasoner smoke test — isolated from the entire Sentinel incident pipeline.

This exists because Problem 2 (Groq/Gemini both returning HTTP 404) showed
that a provider failure can otherwise only be observed indirectly, through a
degraded RCA hours into a demo. This script proves the ONE thing that
matters before anything else is debugged:

    configuration -> client init -> endpoint -> model -> request -> response

using the exact same `Settings` -> `build_reasoner()` -> `Reasoner.complete_json()`
path the real incident pipeline uses — no incident, no evidence, no RCA, just
"can this provider answer a one-line prompt right now".

Usage:
    cd sentinel-ai
    python -m scripts.reasoner_smoke_test
    # or force a specific provider regardless of .env:
    LLM_PROVIDER=groq python -m scripts.reasoner_smoke_test
    LLM_PROVIDER=gemini python -m scripts.reasoner_smoke_test

Exit code is 0 on success, 1 on failure — safe to use as a pre-flight check
in a demo run script or CI job. Never prints the API key.
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    from app.core.config import settings
    from app.reasoning.factory import build_reasoner

    provider = settings.llm_provider
    print(f"provider={provider}")

    reasoner = build_reasoner(settings)
    if reasoner is None:
        print(
            f"FAILED: no Reasoner constructed for provider={provider!r} "
            "(missing API key for this provider — see Settings.<provider>_api_key)"
        )
        return 1

    print(f"label={reasoner.label}")
    result = await reasoner.complete_json(
        system_prompt="Respond with a JSON object.",
        user_prompt='Respond with exactly: {"answer": "OK"}',
    )

    if result is None:
        health = getattr(reasoner, "health", None)
        snapshot = health.snapshot() if health else {}
        print("FAILED: provider call did not return a completion")
        print(f"last_status_code={snapshot.get('last_status_code')}")
        print(f"last_error={snapshot.get('last_error')}")
        print(
            "See the structured log line just above this "
            "(gemini_call_http_error / openai_call_failed) for the exact "
            "endpoint, model, and provider error body."
        )
        return 1

    print(f"response={result!r}")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
