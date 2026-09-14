"""
`Reasoner` — the provider-neutral AI interface.

Before this module existed, `lifecycle/rca.py` imported `openai.AsyncOpenAI`
directly. That was fine when OpenAI-compatible was the only shape needed
(Groq/OpenRouter already worked via `openai_base_url`), but it meant business
logic knew about a specific SDK. This interface is the seam spec section 8/18
asks for:

    Reasoner
       |
       +-- OpenAIReasoner        (also serves Groq/OpenRouter/any
       |                          OpenAI-compatible endpoint, unchanged
       |                          from the original behaviour)
       +-- GeminiReasoner
       |
       +-- LocalSentinelReasoner (future — a fine-tuned model trained on
                                   the incident dataset IncidentMemory is
                                   already collecting; see lifecycle/rca.py
                                   and lifecycle/learning.py)

CRITICAL: this interface is intentionally narrow. `analyze()` returns a raw
string (expected to be a JSON object) and NOTHING ELSE — no tool use, no
function calling, no ability to reach any client that could touch the
cluster. The trust boundary lives in `rca.apply_llm_response()`, unchanged by
this refactor: whatever a Reasoner returns is still validated against the
rule engine's own conclusion before any of it is used, and an action can
still never come from here. Do not add capabilities to this interface without
re-reading that function first.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Reasoner(ABC):
    """One method: given a system prompt and a user prompt, return the raw
    model response text, or None if the call could not be made at all
    (missing key, network error, SDK unavailable, etc — see each
    implementation for what it logs). Never raises: every implementation is
    responsible for its own fail-soft behaviour, matching every other
    collector in this codebase (see lifecycle/investigation.py's docstring).
    """

    #: Short label for logs/incident records — e.g. "openai:gpt-4o-mini" or
    #: "gemini:gemini-2.0-flash". Never influences behaviour, human-readable
    #: only.
    label: str = "unconfigured"

    @abstractmethod
    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        """Ask the model for a JSON-object response. Return the raw text, or
        None on any failure. Callers must still validate/parse the result —
        "returned text" does not mean "returned valid JSON that agrees with
        the rules".
        """
        raise NotImplementedError
