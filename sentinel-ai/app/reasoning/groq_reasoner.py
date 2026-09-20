"""
GroqReasoner — Groq through the same `Reasoner` abstraction.

Groq exposes an OpenAI-compatible chat-completions API, so this is a thin
subclass of OpenAIReasoner that fixes the base URL and labels itself
`groq:<model>`. Nothing outside app/reasoning/ knows Groq exists: switching
provider is `LLM_PROVIDER=groq` (+ `GROQ_API_KEY`), and switching back is the
same one-line change. It is deliberately NOT special-cased anywhere in the
lifecycle.
"""
from __future__ import annotations

from app.reasoning.openai_reasoner import OpenAIReasoner

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqReasoner(OpenAIReasoner):
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        base_url: str = GROQ_BASE_URL,
    ) -> None:
        super().__init__(api_key=api_key, model=model, timeout=timeout, base_url=base_url)
        self.label = f"groq:{model}"
        self.provider_name = "groq"
