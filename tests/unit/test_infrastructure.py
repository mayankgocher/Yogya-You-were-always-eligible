"""Retrieval, the LLM layer, the store, the audit trail and the portal stub."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from yogya.audit import entry, render, reset_entry_ids
from yogya.llm.client import LLMError, _extract_json, get_llm
from yogya.llm.fake import FakeLLM
from yogya.models.application import Application, ApplicationStatus
from yogya.models.household import Occupation, RationCardType, Residence
from yogya.portal import SimulatedPortal, approved, rejected
from yogya.retrieval.index import SchemeRetriever, tokenize
from yogya.store import CaseStore


# ══ Retrieval ══════════════════════════════════════════════════════════════


def test_tokenizer_handles_devanagari():
    assert "वृद्धावस्था" in tokenize("UP वृद्धावस्था Pension")


def test_query_reflects_household_signals(household):
    query = SchemeRetriever.build_query(household)
    assert "widow" in query
    assert "disability" in query
    assert "scholarship" in query
    assert "uttar_pradesh" in query


def test_query_includes_occupation_vocabulary(household):
    household.members[0].occupation = Occupation.STREET_VENDOR
    assert "street vendor" in SchemeRetriever.build_query(household)


def test_retrieval_is_exhaustive_over_applicable_schemes(corpus, household):
    """Recall is everything here. A scheme missed by a keyword is a family who
    never hears about it — so retrieval only *orders*, it does not filter."""
    retriever = SchemeRetriever(corpus)
    retrieved = {c.scheme.scheme_id for c in retriever.retrieve(household)}
    applicable = {s.scheme_id for s in corpus.for_household(household.state)}
    assert retrieved == applicable


def test_retrieval_excludes_other_states_schemes(corpus, household):
    household.state = "bihar"
    retrieved = {c.scheme.scheme_id for c in SchemeRetriever(corpus).retrieve(household)}
    assert not any(sid.startswith("up-") for sid in retrieved)


def test_relevant_schemes_rank_above_irrelevant_ones(corpus, household):
    ranked = [c.scheme.scheme_id for c in SchemeRetriever(corpus).retrieve(household)]
    assert ranked.index("up-widow-pension") < ranked.index("pm-svanidhi")


def test_free_text_search_finds_by_local_vocabulary(corpus):
    hits = {c.scheme.scheme_id for c in SchemeRetriever(corpus).search("vidhwa pension")}
    assert "up-widow-pension" in hits or "nsap-ignwps" in hits


def test_search_for_nonsense_returns_nothing(corpus):
    assert SchemeRetriever(corpus).search("xyzzy plugh") == []


# ══ LLM layer ══════════════════════════════════════════════════════════════


class Sample(BaseModel):
    name: str
    count: int = 0
    tags: list[str] = []


def test_json_is_extracted_from_a_fenced_block():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_is_extracted_from_surrounding_prose():
    assert _extract_json('Sure! Here you go: {"a": 1} Hope that helps.') == {"a": 1}


def test_unparseable_output_raises():
    with pytest.raises(LLMError, match="no JSON"):
        _extract_json("I would rather not.")


def test_fake_synthesises_a_valid_instance_of_any_schema():
    result = FakeLLM().structured(system="s", user="u", schema=Sample)
    assert isinstance(result, Sample)
    assert result.count == 0


def test_fake_applies_a_scripted_override():
    llm = FakeLLM()
    llm.script(Sample, lambda s, u: {"name": "scripted", "count": 7})
    result = llm.structured(system="s", user="u", schema=Sample)
    assert (result.name, result.count) == ("scripted", 7)


def test_fake_records_calls_for_assertions():
    llm = FakeLLM()
    llm.structured(system="sys", user="usr", schema=Sample)
    llm.text(system="sys", user="usr")
    assert len(llm.calls_for(Sample)) == 1
    assert llm.calls[1].kind == "text"


def test_fake_never_touches_the_network(monkeypatch):
    """If FakeLLM ever imported litellm, an offline CI run would start failing
    for reasons no one would connect to this file."""
    import sys

    monkeypatch.setitem(sys.modules, "litellm", None)
    FakeLLM().structured(system="s", user="u", schema=Sample)


def test_offline_setting_yields_the_fake(settings):
    assert isinstance(get_llm(settings), FakeLLM)


def test_missing_api_key_degrades_to_offline(monkeypatch, settings):
    """Deploying without a key must produce a working app, not a broken one."""
    from yogya.config import Settings

    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "TOGETHER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert isinstance(get_llm(Settings(llm_offline=False)), FakeLLM)


def test_a_configured_key_yields_the_real_client(monkeypatch):
    from yogya.config import Settings
    from yogya.llm.client import LLMClient

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    assert isinstance(get_llm(Settings(llm_offline=False)), LLMClient)


# ══ Audit ══════════════════════════════════════════════════════════════════


def test_entry_ids_are_sequential_and_resettable():
    reset_entry_ids()
    first = entry(household_id="hh", actor="a", action="x")
    second = entry(household_id="hh", actor="a", action="y")
    assert (first.entry_id, second.entry_id) == ("audit-000001", "audit-000002")


def test_citations_are_deduplicated_and_sorted():
    e = entry(
        household_id="hh",
        actor="a",
        action="x",
        citations=["cl. 2", "cl. 1", "cl. 2"],
    )
    assert e.citations == ["cl. 1", "cl. 2"]


def test_rendered_trail_shows_actor_action_and_citations():
    entries = [
        entry(
            household_id="hh",
            actor="entitlement_matcher",
            action="evaluated eligibility",
            detail="2/2 criteria passed",
            scheme_id="nfsa-phh",
            rule_version="1.0.0",
            citations=["NFSA 2013, s. 3(1)"],
        )
    ]
    text = render(entries)
    assert "entitlement_matcher" in text
    assert "nfsa-phh @ 1.0.0" in text
    assert "cited: NFSA 2013, s. 3(1)" in text


# ══ Store ══════════════════════════════════════════════════════════════════


def test_profile_round_trips(case_store, household):
    case_store.save_profile(household)
    loaded = case_store.load_profile(household.household_id)
    assert loaded.head_name == household.head_name
    assert len(loaded.members) == len(household.members)


def test_missing_profile_is_none(case_store):
    assert case_store.load_profile("nobody") is None


def test_applications_merge_rather_than_replace(case_store):
    """A partial second run must not wipe the first run's applications."""
    first = Application(
        application_id="a1", household_id="hh", scheme_id="s1", scheme_name="S1"
    )
    second = Application(
        application_id="a2", household_id="hh", scheme_id="s2", scheme_name="S2"
    )
    case_store.save_applications("hh", [first])
    case_store.save_applications("hh", [second])
    assert {a.application_id for a in case_store.load_applications("hh")} == {"a1", "a2"}


def test_saving_the_same_application_updates_it(case_store):
    application = Application(
        application_id="a1", household_id="hh", scheme_id="s1", scheme_name="S1"
    )
    case_store.save_applications("hh", [application])
    application.status = ApplicationStatus.APPROVED
    case_store.save_applications("hh", [application])

    stored = case_store.load_applications("hh")
    assert len(stored) == 1
    assert stored[0].status is ApplicationStatus.APPROVED


def test_audit_is_append_only(case_store):
    case_store.append_audit("hh", [entry(household_id="hh", actor="a", action="1")])
    case_store.append_audit("hh", [entry(household_id="hh", actor="a", action="2")])
    assert [e.action for e in case_store.load_audit("hh")] == ["1", "2"]


def test_known_fields_exclude_values_the_profiler_was_unsure_about(case_store, household):
    """Only *confirmed* values may pre-fill a later intake. Carrying a guess
    forward would launder it into a fact."""
    household.annual_income = 48000
    household.unknown_fields = ["annual_income"]
    case_store.save_profile(household)
    known = case_store.known_fields(household.household_id)
    assert "annual_income" not in known
    assert known["state"] == "uttar_pradesh"


def test_known_fields_exclude_unknown_enums(case_store, sparse_household):
    case_store.save_profile(sparse_household)
    known = case_store.known_fields(sparse_household.household_id)
    assert "ration_card" not in known
    assert "residence" not in known


def test_case_survives_a_new_store_facade_over_the_same_backend(household):
    """The case belongs to the store, not to a session object."""
    from langgraph.store.memory import InMemoryStore

    backend = InMemoryStore()
    CaseStore(backend).save_profile(household)
    assert CaseStore(backend).load_profile(household.household_id) is not None


# ══ Portal stub ════════════════════════════════════════════════════════════


def test_reference_numbers_are_stable_for_the_same_application():
    portal = SimulatedPortal()
    first = portal.submit("app-x", "s", {}).reference_number
    second = portal.submit("app-x", "s", {}).reference_number
    assert first == second


def test_scripted_outcomes_advance_with_the_attempt():
    portal = SimulatedPortal()
    portal.script("app-1", rejected("no"), rejected("still no"), approved("yes"))
    assert portal.poll("app-1", "s", attempt=0).status == "rejected"
    assert portal.poll("app-1", "s", attempt=2).status == "approved"


def test_the_last_scripted_outcome_repeats():
    portal = SimulatedPortal()
    portal.script("app-1", rejected("no"))
    assert portal.poll("app-1", "s", attempt=9).status == "rejected"


def test_unscripted_outcomes_are_deterministic():
    a = SimulatedPortal(rejection_rate=0.5).poll("app-z", "s").status
    b = SimulatedPortal(rejection_rate=0.5).poll("app-z", "s").status
    assert a == b


def test_zero_rejection_rate_approves_everything():
    portal = SimulatedPortal(rejection_rate=0.0)
    assert all(
        portal.poll(f"app-{i}", "s").status == "approved" for i in range(30)
    )
