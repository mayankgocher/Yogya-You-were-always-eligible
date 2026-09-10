"""Corpus loading, version resolution, and the shape of the shipped data."""

from __future__ import annotations

import json
from datetime import date

import pytest

from yogya.data.loader import CorpusError, SchemeCorpus
from yogya.models.household import DocumentKind
from yogya.models.scheme import Scheme


# ── loading ────────────────────────────────────────────────────────────────


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(CorpusError, match="not found"):
        SchemeCorpus.from_dir(tmp_path / "nope")


def test_empty_directory_is_an_error(tmp_path):
    with pytest.raises(CorpusError, match="no reviewed corpus files"):
        SchemeCorpus.from_dir(tmp_path)


def test_pending_ingests_are_never_loaded(tmp_path):
    """An unreviewed auto-extracted rule must not decide anyone's eligibility."""
    reviewed = {
        "snapshot_id": "s1",
        "schemes": [
            {
                "scheme_id": "good",
                "name": "Reviewed Scheme",
                "benefit_summary": "x",
                "criteria": [],
            }
        ],
    }
    pending = {
        "snapshot_id": "s2",
        "schemes": [
            {
                "scheme_id": "unreviewed",
                "name": "Auto Scheme",
                "benefit_summary": "x",
                "criteria": [],
            }
        ],
    }
    (tmp_path / "reviewed.json").write_text(json.dumps(reviewed))
    (tmp_path / "fresh.pending.json").write_text(json.dumps(pending))

    corpus = SchemeCorpus.from_dir(tmp_path)
    assert [s.scheme_id for s in corpus.current()] == ["good"]


def test_duplicate_rule_versions_are_rejected(tmp_path):
    payload = {
        "snapshot_id": "s1",
        "schemes": [
            {"scheme_id": "dup", "name": "A", "benefit_summary": "x", "rule_version": "1.0.0"},
            {"scheme_id": "dup", "name": "B", "benefit_summary": "x", "rule_version": "1.0.0"},
        ],
    }
    (tmp_path / "c.json").write_text(json.dumps(payload))
    with pytest.raises(CorpusError, match="duplicate rule version"):
        SchemeCorpus.from_dir(tmp_path)


def test_state_is_inherited_from_the_file(tmp_path):
    payload = {
        "snapshot_id": "s",
        "state": "bihar",
        "schemes": [
            {"scheme_id": "x", "name": "X", "benefit_summary": "y", "level": "state"}
        ],
    }
    (tmp_path / "bihar.json").write_text(json.dumps(payload))
    assert SchemeCorpus.from_dir(tmp_path).get("x").state == "bihar"


# ── versioning ─────────────────────────────────────────────────────────────


def test_both_versions_of_the_pension_are_present(corpus):
    assert corpus.versions_of("up-old-age-pension") == ["1.0.0", "1.1.0"]


def test_current_returns_only_the_version_in_force(corpus):
    current = [s for s in corpus.current() if s.scheme_id == "up-old-age-pension"]
    assert len(current) == 1
    assert current[0].rule_version == "1.1.0"


def test_a_past_date_resolves_to_the_superseded_version(corpus):
    """A rejection from 2023 must be reviewable against 2023's rules.

    Re-checking it against today's higher income ceiling would manufacture a
    wrongful denial that never happened.
    """
    in_2023 = [
        s for s in corpus.current(date(2023, 6, 1)) if s.scheme_id == "up-old-age-pension"
    ]
    assert in_2023[0].rule_version == "1.0.0"


def test_explicit_version_lookup(corpus):
    assert corpus.get("up-old-age-pension", version="1.0.0").rule_version == "1.0.0"


def test_unknown_version_raises(corpus):
    with pytest.raises(KeyError, match="no rule version"):
        corpus.get("up-old-age-pension", version="9.9.9")


def test_unknown_scheme_raises(corpus):
    with pytest.raises(KeyError, match="unknown scheme"):
        corpus.get("does-not-exist")


def test_income_ceiling_actually_rose_between_versions(corpus):
    """Guards the fixture that the versioning tests depend on."""
    old = corpus.get("up-old-age-pension", version="1.0.0")
    new = corpus.get("up-old-age-pension", version="1.1.0")
    old_ceiling = next(c.value for c in old.criteria if c.field == "annual_income")
    new_ceiling = next(c.value for c in new.criteria if c.field == "annual_income")
    assert new_ceiling > old_ceiling


# ── applicability ──────────────────────────────────────────────────────────


def test_state_schemes_are_scoped_to_their_state(corpus):
    up = {s.scheme_id for s in corpus.for_household("uttar_pradesh")}
    bihar = {s.scheme_id for s in corpus.for_household("bihar")}
    assert "up-kanya-sumangala" in up
    assert "up-kanya-sumangala" not in bihar


def test_central_schemes_apply_everywhere(corpus):
    for state in ("uttar_pradesh", "bihar", "kerala"):
        ids = {s.scheme_id for s in corpus.for_household(state)}
        assert "pm-kisan" in ids
        assert "pmjay" in ids


# ── the shipped corpus itself ──────────────────────────────────────────────


def test_shipped_corpus_is_not_trivially_small(corpus):
    assert len(corpus) >= 20


def test_every_scheme_has_criteria_and_a_source(corpus):
    """A scheme with no criteria would be reported eligible for everyone."""
    for scheme in corpus.current():
        assert scheme.criteria, f"{scheme.scheme_id} has no eligibility criteria"
        assert scheme.source_url, f"{scheme.scheme_id} has no source URL"
        assert scheme.benefit_summary


def test_every_criterion_names_a_field_the_engine_knows(corpus, household):
    known = set(household.rule_context())
    for scheme in corpus.all_versions:
        for criterion in scheme.criteria:
            assert criterion.field in known, (
                f"{scheme.scheme_id}/{criterion.criterion_id} uses unknown field "
                f"{criterion.field!r}"
            )


def test_every_required_document_is_one_we_can_advise_on(corpus):
    """If we tell a family a paper is missing, we must be able to say where
    to get it. An unmapped document kind is a dead end on screen."""
    from yogya.agents.documents import DOCUMENT_GUIDANCE

    for scheme in corpus.current():
        for document in scheme.required_documents:
            assert document in DOCUMENT_GUIDANCE, (
                f"{scheme.scheme_id} requires {document.value} but there is no "
                "guidance on how to obtain it"
            )


def test_document_kinds_round_trip(corpus):
    for scheme in corpus.current():
        for document in scheme.required_documents:
            assert isinstance(document, DocumentKind)


def test_schemes_validate_as_models(corpus):
    for scheme in corpus.all_versions:
        assert isinstance(scheme, Scheme)
        Scheme.model_validate(scheme.model_dump())
