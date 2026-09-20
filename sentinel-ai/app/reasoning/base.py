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

from app.reasoning.health import ReasonerHealth


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

    # Provider health (see reasoning/health.py). Created lazily so a Reasoner
    # subclass — including a test double — that never calls super().__init__()
    # still works; the factory replaces it with one configured from Settings.
    _health: ReasonerHealth | None = None

    @property
    def health(self) -> ReasonerHealth:
        if self._health is None:
            self._health = ReasonerHealth()
        return self._health

    @health.setter
    def health(self, value: ReasonerHealth) -> None:
        self._health = value

    def report_failure(self, detail: str, status_code: int | None = None) -> None:
        """Called by implementations wherever they return None because the
        provider call failed. Never raises."""
        self.health.record_failure(detail, status_code)

    def report_success(self) -> None:
        self.health.record_success()

    @abstractmethod
    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        """Ask the model for a JSON-object response. Return the raw text, or
        None on any failure. Callers must still validate/parse the result —
        "returned text" does not mean "returned valid JSON that agrees with
        the rules".
        """
        raise NotImplementedError
