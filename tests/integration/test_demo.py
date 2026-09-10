"""The demo case, treated as a regression test.

`python -m yogya.demo` is what gets run in front of people, so it is pinned
here. If a change to the corpus, the rules or the graph alters the story the
demo tells, this file fails and you find out before the demo does.

The assertions are on *invariants and relationships*, not on every number —
adding a scheme to the corpus should not break the build, but silently losing
the appeal path should.
"""

from __future__ import annotations

import pytest

from yogya.demo import demo_household, demo_llm, demo_portal, run_demo
from yogya.models.application import ApplicationStatus
from yogya.models.scheme import EligibilityVerdict
from yogya.rules.engine import evaluate_scheme

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def final():
    return run_demo(verbose=False)


def test_the_demo_runs_offline_and_completes(final):
    assert final["summary"]
    assert final["summary"]["household_id"] == "hh-sunita-devi"


def test_the_family_qualifies_for_many_more_benefits_than_they_receive(final):
    """The premise of the project, stated as an assertion."""
    summary = final["summary"]
    assert summary["benefits_discovered"] >= 12
    assert summary["already_receiving"] == 1


def test_the_profile_is_extracted_from_the_hinglish_notes(final):
    household = final["household"]
    assert household.head_name == "Sunita Devi"
    assert household.state == "uttar_pradesh"
    assert household.annual_income == 48000
    assert len(household.members) == 5
    assert household.unknown_fields == []


def test_every_branch_of_the_lifecycle_is_exercised(final):
    """The demo is only worth showing if it reaches every state."""
    statuses = {a.status for a in final["applications"]}
    assert ApplicationStatus.APPROVED in statuses
    assert ApplicationStatus.BLOCKED_ON_DOCUMENTS in statuses
    assert ApplicationStatus.ESCALATED in statuses


def test_one_wrongful_denial_is_recovered_on_appeal(final):
    summary = final["summary"]
    assert summary["wrongful_denials_recovered"] == 1

    recovered = [
        a
        for a in final["applications"]
        if a.status is ApplicationStatus.APPROVED and a.appeal_attempts
    ]
    assert len(recovered) == 1
    assert recovered[0].scheme_id == "nsap-ignoaps"
    assert recovered[0].appeals[0].outcome == "upheld"
    assert recovered[0].appeals[0].citations


def test_the_exhausted_case_is_escalated_not_dropped(final):
    escalated = [
        a for a in final["applications"] if a.status is ApplicationStatus.ESCALATED
    ]
    assert len(escalated) == 1
    assert escalated[0].scheme_id == "pmuy"
    assert escalated[0].appeal_attempts == 2


def test_blocked_applications_explain_how_to_unblock_themselves(final):
    blocked = [
        a
        for a in final["applications"]
        if a.status is ApplicationStatus.BLOCKED_ON_DOCUMENTS
    ]
    assert blocked
    for application in blocked:
        assert application.blocking_gaps
        for gap in application.blocking_gaps:
            assert gap.where_to_get
            assert gap.typical_days is not None


def test_insurance_cover_is_reported_separately_from_cash(final):
    """A family cannot spend a sum insured. Reporting Rs 9 lakh of cover as
    'recovered' would be the kind of number that wins a demo and fails an
    audit."""
    summary = final["summary"]
    assert summary["rupees_recovered_annual"] > 0
    assert summary["insurance_cover_secured_inr"] > summary["rupees_recovered_annual"]


def test_no_form_field_was_invented(final):
    """A complete profile should produce no flagged fields — and every field
    that *is* flagged must be blank, never guessed."""
    from yogya.agents.forms import FLAG

    assert final["summary"]["form_error_rate"] == 0.0
    for application in final["applications"]:
        for value in application.form_data.values():
            assert value != "" or value == FLAG


def test_the_caseworker_was_asked_before_every_submission(final):
    """One gate per filed application, plus one per appeal, plus the two at
    the start. Nothing reached a portal unattended."""
    submitted = [
        a
        for a in final["applications"]
        if a.status
        not in {ApplicationStatus.BLOCKED_ON_DOCUMENTS, ApplicationStatus.ABANDONED}
    ]
    appeals = sum(a.appeal_attempts for a in final["applications"])
    assert final["summary"]["human_interrupts"] == 2 + len(submitted) + appeals


def test_every_decision_is_traceable(final):
    """The audit trail must cover every scheme that was evaluated."""
    evaluated = {
        e.scheme_id
        for e in final["audit"]
        if e.action.startswith("evaluated eligibility")
    }
    assert evaluated == {r.scheme_id for r in final["eligibility"]}
    for e in final["audit"]:
        if e.action.startswith("evaluated eligibility"):
            assert e.citations and e.rule_version


def test_the_demo_is_reproducible():
    """Two runs, byte-identical verdicts. A demo that shifts between runs is
    a demo you cannot rehearse."""
    first = run_demo(verbose=False)
    second = run_demo(verbose=False)
    assert first["summary"] == second["summary"]
    assert [
        (a.scheme_id, a.status, a.appeal_attempts) for a in first["applications"]
    ] == [(a.scheme_id, a.status, a.appeal_attempts) for a in second["applications"]]


def test_the_scripted_profile_matches_what_the_rules_are_told(corpus):
    """Guards the demo's own fixture: the FakeLLM must produce exactly the
    household the demo claims to describe, or every downstream number is about
    a different family."""
    from yogya.agents.profiler import HouseholdProfiler
    from yogya.demo import HOUSEHOLD_ID, INTAKE_NOTES

    extracted, _ = HouseholdProfiler(demo_llm()).run(
        household_id=HOUSEHOLD_ID, intake_notes=INTAKE_NOTES
    )
    expected = demo_household()
    assert extracted.rule_context() == expected.rule_context()
    assert extracted.available_documents == expected.available_documents


def test_the_demo_family_really_is_eligible_for_what_is_claimed(corpus):
    """Independent of the graph: evaluate the demo household directly."""
    household = demo_household()
    eligible = [
        s.scheme_id
        for s in corpus.for_household(household.state)
        if evaluate_scheme(household, s).verdict is EligibilityVerdict.ELIGIBLE
    ]
    for scheme_id in ("nfsa-phh", "nsap-ignoaps", "nsap-ignwps", "pmay-g", "pmuy"):
        assert scheme_id in eligible


def test_the_portal_script_covers_the_applications_it_names():
    """A typo in an application id would silently disable the appeal story."""
    from yogya.demo import app_id

    portal = demo_portal()
    assert set(portal.scripted) == {app_id("nsap-ignoaps"), app_id("pmuy")}
