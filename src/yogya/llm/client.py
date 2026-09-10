"""Provider-agnostic LLM access via LiteLLM.

One narrow interface — `LLMClient.structured()` — is all the agents get. That
constraint is what keeps the system testable: every agent's LLM dependency is a
function from (prompt, schema) to a validated Pydantic object, so the fake in
`yogya.llm.fake` is a complete substitute rather than an approximation.

Provider is a config string. `groq/llama-3.3-70b-versatile`,
`gemini/gemini-2.0-flash`, `ollama/llama3.2` and `openai/gpt-4o-mini` all work
without a code change, which matters when you are deploying on whichever free
tier still has capacity.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from yogya.config import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when the model could not produce a usable structured answer."""


def _extract_json(text: str) -> Any:
    """Pull JSON out of a response that may be wrapped in prose or fences.

    Small models fenced in by a schema still like to say "Here is the JSON:".
    Rather than fight that with prompt engineering alone, we parse defensively.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if not match:
        raise LLMError(f"no JSON object found in model output: {text[:200]!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"malformed JSON in model output: {text[:200]!r}") from exc


class LLMClient:
    """Thin, retrying, schema-validating wrapper over litellm.completion."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---- public API -------------------------------------------------------
    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        """Ask the model for one object of type `schema` and validate it."""
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"{user}\n\n"
                    "Respond with a single JSON object only — no prose, no code "
                    "fence, no explanation outside the JSON. It must validate "
                    "against this JSON Schema:\n"
                    f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
                ),
            },
        ]
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                raw = self._complete(messages, temperature, max_tokens)
                return schema.model_validate(_extract_json(raw))
            except (LLMError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "structured() attempt %s/%s failed: %s",
                    attempt + 1,
                    self.settings.llm_max_retries + 1,
                    exc,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That response was rejected: {exc}. "
                            "Return corrected JSON only."
                        ),
                    }
                )
        raise LLMError(f"structured generation failed after retries: {last_error}")

    def text(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        """Free-form generation, used for letters the human will edit anyway."""
        return self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            None,
            max_tokens,
        )

    # ---- transport --------------------------------------------------------
    def _complete(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        import litellm  # imported lazily: keeps import time and test cost down

        models = [self.settings.llm_model, *self.settings.llm_fallback_models]
        last_error: Exception | None = None
        for model in models:
            try:
                response = litellm.completion(
                    model=model,
                    messages=list(messages),
                    temperature=(
                        self.settings.llm_temperature
                        if temperature is None
                        else temperature
                    ),
                    max_tokens=max_tokens or self.settings.llm_max_tokens,
                    timeout=self.settings.llm_timeout_s,
                )
                return response["choices"][0]["message"]["content"] or ""
            except Exception as exc:  # noqa: BLE001 — provider errors are opaque
                last_error = exc
                logger.warning("provider %s failed: %s", model, exc)
        raise LLMError(f"all providers failed; last error: {last_error}")


def get_llm(settings: Settings | None = None):
    """Return the client the app should use.

    Offline mode is not a test-only affordance — it is how the app degrades on
    a free tier that has run out of quota. Every agent has a deterministic
    fallback, so Yogya stays useful (rules still decide, forms still fill)
    with no model at all; it only loses its natural-language polish.
    """
    import os

    settings = settings or get_settings()

    # No key configured means no provider will answer. Falling back here rather
    # than at the first call keeps the failure at startup, where it is visible,
    # instead of halfway through a family's screening.
    provider_keys = (
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TOGETHER_API_KEY",
        "OPENROUTER_API_KEY",
    )
    has_key = any(os.environ.get(k) for k in provider_keys)
    is_local = settings.llm_model.startswith(("ollama/", "ollama_chat/"))

    if settings.llm_offline or not (has_key or is_local):
        from yogya.llm.fake import FakeLLM

        if not settings.llm_offline:
            logger.warning(
                "no provider API key found for model %r — running offline. "
                "Rule-based eligibility, document guidance and form filling are "
                "unaffected; only natural-language wording falls back to "
                "templates.",
                settings.llm_model,
            )
        return FakeLLM()
    return LLMClient(settings)
