"""The real LiteLLM-backed client, with litellm itself stubbed out.

No test in this suite is allowed to make a network call, so `litellm` is
replaced by a fake module. What is being tested is our own behaviour around the
provider: retrying on malformed output, falling back between providers, and
refusing to return an object that does not validate.
"""

from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from yogya.config import Settings
from yogya.llm.client import LLMClient, LLMError


class Answer(BaseModel):
    verdict: str
    score: int


def install_fake_litellm(monkeypatch, responses, record=None):
    """Replace the litellm module with one that returns scripted content."""
    queue = list(responses)

    def completion(**kwargs):
        if record is not None:
            record.append(kwargs)
        item = queue.pop(0) if queue else queue[-1] if queue else ""
        if isinstance(item, Exception):
            raise item
        return {"choices": [{"message": {"content": item}}]}

    module = types.ModuleType("litellm")
    module.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


@pytest.fixture
def client() -> LLMClient:
    return LLMClient(Settings(llm_model="groq/test", llm_max_retries=2))


# ── happy path ─────────────────────────────────────────────────────────────


def test_a_clean_json_response_validates(monkeypatch, client):
    install_fake_litellm(monkeypatch, ['{"verdict": "eligible", "score": 3}'])
    answer = client.structured(system="s", user="u", schema=Answer)
    assert (answer.verdict, answer.score) == ("eligible", 3)


def test_a_fenced_response_is_still_parsed(monkeypatch, client):
    install_fake_litellm(
        monkeypatch, ['```json\n{"verdict": "x", "score": 1}\n```']
    )
    assert client.structured(system="s", user="u", schema=Answer).score == 1


def test_the_schema_is_sent_to_the_model(monkeypatch, client):
    record = []
    install_fake_litellm(monkeypatch, ['{"verdict": "x", "score": 1}'], record)
    client.structured(system="sys prompt", user="usr prompt", schema=Answer)

    messages = record[0]["messages"]
    assert messages[0] == {"role": "system", "content": "sys prompt"}
    assert "usr prompt" in messages[1]["content"]
    assert "JSON Schema" in messages[1]["content"]
    assert "verdict" in messages[1]["content"]


def test_free_text_generation_returns_the_content(monkeypatch, client):
    install_fake_litellm(monkeypatch, ["Dear Sir/Madam,"])
    assert client.text(system="s", user="u").startswith("Dear")


# ── retrying ───────────────────────────────────────────────────────────────


def test_malformed_output_is_retried_with_the_error(monkeypatch, client):
    """Small models fail the schema and then fix it when told what broke."""
    record = []
    install_fake_litellm(
        monkeypatch,
        ["not json at all", '{"verdict": "ok", "score": 2}'],
        record,
    )
    answer = client.structured(system="s", user="u", schema=Answer)
    assert answer.score == 2
    assert len(record) == 2
    assert "rejected" in record[1]["messages"][-1]["content"]


def test_output_that_fails_validation_is_retried(monkeypatch, client):
    install_fake_litellm(
        monkeypatch,
        ['{"verdict": "ok"}', '{"verdict": "ok", "score": 5}'],  # missing field
    )
    assert client.structured(system="s", user="u", schema=Answer).score == 5


def test_retries_are_bounded(monkeypatch):
    client = LLMClient(Settings(llm_model="groq/test", llm_max_retries=1))
    record = []
    install_fake_litellm(monkeypatch, ["nope", "still nope", "nope again"], record)
    with pytest.raises(LLMError, match="failed after retries"):
        client.structured(system="s", user="u", schema=Answer)
    assert len(record) == 2  # the initial attempt plus one retry


# ── provider fallback ──────────────────────────────────────────────────────


def test_a_failing_provider_falls_through_to_the_next(monkeypatch):
    """Free tiers run out of quota mid-afternoon. A second provider in the
    config is the difference between a degraded run and a dead one."""
    client = LLMClient(
        Settings(
            llm_model="groq/primary",
            llm_fallback_models=["gemini/secondary"],
        )
    )
    record = []
    install_fake_litellm(
        monkeypatch,
        [RuntimeError("rate limited"), '{"verdict": "ok", "score": 1}'],
        record,
    )
    assert client.structured(system="s", user="u", schema=Answer).score == 1
    assert [call["model"] for call in record] == ["groq/primary", "gemini/secondary"]


def test_all_providers_failing_raises(monkeypatch):
    client = LLMClient(
        Settings(llm_model="a/b", llm_fallback_models=["c/d"], llm_max_retries=0)
    )
    install_fake_litellm(
        monkeypatch, [RuntimeError("down"), RuntimeError("also down")]
    )
    with pytest.raises(LLMError):
        client.structured(system="s", user="u", schema=Answer)


def test_generation_settings_are_passed_through(monkeypatch):
    client = LLMClient(
        Settings(llm_model="groq/test", llm_temperature=0.0, llm_max_tokens=99)
    )
    record = []
    install_fake_litellm(monkeypatch, ['{"verdict": "x", "score": 0}'], record)
    client.structured(system="s", user="u", schema=Answer)
    assert record[0]["temperature"] == 0.0
    assert record[0]["max_tokens"] == 99


def test_an_explicit_temperature_overrides_the_setting(monkeypatch):
    client = LLMClient(Settings(llm_model="groq/test", llm_temperature=0.0))
    record = []
    install_fake_litellm(monkeypatch, ['{"verdict": "x", "score": 0}'], record)
    client.structured(system="s", user="u", schema=Answer, temperature=0.7)
    assert record[0]["temperature"] == 0.7


def test_an_empty_response_is_treated_as_malformed(monkeypatch, client):
    install_fake_litellm(monkeypatch, ["", '{"verdict": "x", "score": 0}'])
    assert client.structured(system="s", user="u", schema=Answer).verdict == "x"
