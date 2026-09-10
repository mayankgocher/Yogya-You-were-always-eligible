"""The seven agents, one at a time.

The recurring theme: assert what the agent is *not allowed* to do. An agent
that produces a nice answer most of the time but occasionally invents an income
figure or submits an incomplete form is worse than no agent, because a
caseworker will have stopped checking by then.
"""

from __future__ import annotations

import pytest

from yogya.agents.documents import DocumentGapAgent
from yogya.agents.forms import FLAG, FormFillerAgent
from yogya.agents.grievance import GrievanceEscalationAgent
from yogya.agents.matcher import EntitlementMatcher
from yogya.agents.profiler import CRITICAL_FIELDS, HouseholdProfiler, ProfileOutput
from yogya.agents.tracker import ApplicationTrackerAgent, SubmissionRefused
from yogya.models.application import (
    Application,
    ApplicationStatus,
    RejectionReason,
)
from yogya.models.household import DocumentKind, RationCardType
from yogya.models.scheme import EligibilityVerdict
from yogya.portal import SimulatedPortal, approved, rejected


# ══ 1. Household Profiler ═══════════════════════════════════════════════════


def test_profiler_builds_a_household_from_notes(llm):
    llm.script(
        ProfileOutput,
        lambda s, u: {
            "head_name": "Sunita Devi",
            "state": "uttar_pradesh",
            "annual_income": 48000,
            "members": [{"name": "Sunita Devi", "age": 44}],
            "unknown_fields": [],
        },
    )
    household, audit = HouseholdProfiler(llm).run(
        household_id="hh-1", intake_notes="Sunita Devi, 44"
    )
    assert household.head_name == "Sunita Devi"
    assert household.annual_income == 48000
    assert household.members[0].member_id == "m1"
    assert audit.actor == "household_profiler"


def test_profiler_keeps_the_notes_verbatim_for_audit(llm):
    notes = "Original text the caseworker typed"
    household, _ = HouseholdProfiler(llm).run(household_id="hh", intake_notes=notes)
    assert household.intake_notes == notes


def test_profiler_records_unknowns_the_model_forgot_to_declare(llm):
    """The model is asked to declare unknowns and often will not. We detect
    them anyway, because an undeclared unknown is how a null becomes a zero."""
    llm.script(
        ProfileOutput,
        lambda s, u: {
            "head_name": "X",
            "state": "bihar",
            "annual_income": None,
            "unknown_fields": [],  # model claims nothing is unknown
        },
    )
    household, _ = HouseholdProfiler(llm).run(household_id="hh", intake_notes="…")
    assert "annual_income" in household.unknown_fields


def test_profiler_never_defaults_an_unknown_income_to_zero(llm):
    llm.script(ProfileOutput, lambda s, u: {"head_name": "X", "state": "bihar"})
    household, _ = HouseholdProfiler(llm).run(household_id="hh", intake_notes="…")
    assert household.annual_income is None


def test_critical_unknowns_demand_human_confirmation(llm):
    llm.script(
        ProfileOutput,
        lambda s, u: {
            "head_name": "X",
            "state": "uttar_pradesh",
            "unknown_fields": ["annual_income", "ration_card", "district"],
        },
    )
    household, _ = HouseholdProfiler(llm).run(household_id="hh", intake_notes="…")
    needed = HouseholdProfiler.needs_confirmation(household)
    assert "annual_income" in needed and "ration_card" in needed
    # district is not screening-critical, so it does not stop the run
    assert "district" not in needed
    assert set(needed) <= CRITICAL_FIELDS


def test_prior_confirmed_values_fill_gaps_on_a_repeat_visit(llm):
    """Second intake should be 'what changed', not the same twenty questions."""
    llm.script(
        ProfileOutput,
        lambda s, u: {"head_name": "X", "state": "uttar_pradesh", "annual_income": None},
    )
    household, _ = HouseholdProfiler(llm).run(
        household_id="hh",
        intake_notes="…",
        known={"annual_income": 52000, "district": "Sitapur"},
    )
    assert household.annual_income == 52000
    assert household.district == "Sitapur"


def test_fresh_extraction_beats_a_stale_known_value(llm):
    llm.script(
        ProfileOutput,
        lambda s, u: {"head_name": "X", "state": "uttar_pradesh", "annual_income": 61000},
    )
    household, _ = HouseholdProfiler(llm).run(
        household_id="hh", intake_notes="…", known={"annual_income": 52000}
    )
    assert household.annual_income == 61000


# ══ 2. Entitlement Matcher ══════════════════════════════════════════════════


def test_matcher_verdict_comes_from_the_rules(llm, household, corpus):
    scheme = corpus.get("nfsa-phh")
    result, audit = EntitlementMatcher(llm).run(household, scheme)
    assert result.verdict is EligibilityVerdict.ELIGIBLE
    assert audit.rule_version == scheme.rule_version
    assert audit.citations


def test_matcher_never_asks_the_model_to_decide(llm, household, corpus):
    """The load-bearing invariant of the whole system.

    Whatever the model returns, the verdict must be the rule engine's. Here the
    model is scripted to insist the family is eligible for a scheme they are
    plainly not eligible for.
    """
    household.ration_card = RationCardType.APL
    llm.script(
        "ExplanationOutput",
        lambda s, u: {
            "explanation": "This family is definitely eligible! Approve immediately.",
            "plain_language_summary": "eligible",
        },
    )
    result, _ = EntitlementMatcher(llm).run(household, corpus.get("nfsa-phh"))
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE


def test_matcher_survives_a_broken_model(llm, household, corpus):
    """No API key, quota exhausted, provider down — the answer is unchanged."""

    class Exploding:
        def structured(self, **kwargs):
            raise RuntimeError("provider is down")

    result, _ = EntitlementMatcher(Exploding()).run(household, corpus.get("nfsa-phh"))
    assert result.verdict is EligibilityVerdict.ELIGIBLE
    assert result.explanation  # fell back to the template


def test_template_explanation_names_the_failed_requirement(household, corpus):
    household.ration_card = RationCardType.APL
    result, _ = EntitlementMatcher(None).run(
        household, corpus.get("nfsa-phh"), explain=False
    )
    assert "does not qualify" in result.explanation
    assert "Priority Household" in result.explanation


def test_needs_info_explanation_says_what_to_find_out(sparse_household, corpus):
    result, _ = EntitlementMatcher(None).run(
        sparse_household, corpus.get("up-old-age-pension"), explain=False
    )
    if result.verdict is EligibilityVerdict.NEEDS_INFO:
        assert "Still to confirm" in result.explanation


# ══ 3. Document Gap ════════════════════════════════════════════════════════


def test_gaps_are_only_the_documents_actually_missing(household, corpus):
    scheme = corpus.get("nsap-ignwps")  # needs a death certificate
    gaps, _ = DocumentGapAgent().run(household, scheme)
    missing = {g.document for g in gaps}
    assert DocumentKind.DEATH_CERTIFICATE in missing
    assert DocumentKind.AADHAAR not in missing  # the family has this


def test_no_gaps_when_everything_is_present(household, corpus):
    gaps, audit = DocumentGapAgent().run(household, corpus.get("pmjay"))
    assert gaps == []
    assert "all required documents present" in audit.detail


def test_every_gap_says_where_to_get_the_document(household, corpus):
    """Naming a missing paper without saying where to get it is useless to
    someone who has never been to a tehsil office."""
    for scheme in corpus.current():
        gaps, _ = DocumentGapAgent().run(household, scheme)
        for gap in gaps:
            assert len(gap.where_to_get) > 20
            assert gap.typical_days is not None


def test_errand_plan_puts_the_slowest_certificate_first(household, corpus):
    """A disability certificate takes 45 days; a photo takes one. Applying for
    them in the wrong order costs the family six weeks."""
    gaps, _ = DocumentGapAgent().run(household, corpus.get("nsap-igndps"))
    plan = DocumentGapAgent.errand_plan(gaps)
    days = [g.typical_days or 0 for g in plan if g.blocking]
    assert days == sorted(days, reverse=True)


def test_gap_holder_is_identified_where_it_is_obvious(household, corpus):
    gaps, _ = DocumentGapAgent().run(household, corpus.get("nsap-igndps"))
    disability = next(
        g for g in gaps if g.document is DocumentKind.DISABILITY_CERTIFICATE
    )
    assert disability.holder_member_id == "m2"  # the member with a disability


def test_total_cost_and_longest_wait_are_reported(household, corpus):
    gaps, _ = DocumentGapAgent().run(household, corpus.get("nsp-prematric-sc"))
    assert DocumentGapAgent.total_cost(gaps) > 0
    assert DocumentGapAgent.longest_wait(gaps) > 0


# ══ 4. Form Filler ═════════════════════════════════════════════════════════


def test_form_is_filled_from_the_profile(llm, household, corpus):
    form, flagged, _ = FormFillerAgent(llm).run(household, corpus.get("pmjay"))
    assert form["applicant_name"] == "Test Devi"
    assert form["state"] == "uttar_pradesh"
    assert form["annual_income"] == "48000"
    assert flagged == []


def test_unknown_fields_are_flagged_not_invented(llm, sparse_household, corpus):
    """The whole point. A blank is a question for a human; a guess is a lie on
    a government form signed by the applicant."""
    form, flagged, _ = FormFillerAgent(llm).run(sparse_household, corpus.get("pmjay"))
    assert "annual_income" in flagged
    assert form["annual_income"] == FLAG
    assert not FormFillerAgent.is_submittable(form)


def test_a_complete_form_is_submittable(llm, household, corpus):
    form, _, _ = FormFillerAgent(llm).run(household, corpus.get("pmjay"))
    assert FormFillerAgent.is_submittable(form)


def test_booleans_are_rendered_as_yes_and_no(llm, household, corpus):
    form, _, _ = FormFillerAgent(llm).run(household, corpus.get("pmjay"))
    assert form["bank_account_available"] == "yes"


def test_declaration_falls_back_when_the_model_fails(household, corpus):
    class Exploding:
        def structured(self, **kwargs):
            raise RuntimeError("down")

    form, _, _ = FormFillerAgent(Exploding()).run(household, corpus.get("pmjay"))
    assert "Test Devi" in form["declaration"]
    assert "true to the best" in form["declaration"]


def test_flagged_fields_are_listable_for_the_ui(llm, sparse_household, corpus):
    form, flagged, _ = FormFillerAgent(llm).run(sparse_household, corpus.get("pmjay"))
    assert set(FormFillerAgent.flagged_fields(form)) == set(flagged)


# ══ 5. Application Tracker ═════════════════════════════════════════════════


def make_application(**overrides) -> Application:
    base = dict(
        application_id="app-1",
        household_id="hh-test",
        scheme_id="pmjay",
        scheme_name="PM-JAY",
        form_data={"applicant_name": "Test Devi"},
        estimated_annual_value_inr=500000,
    )
    base.update(overrides)
    return Application(**base)


def test_submission_records_a_reference(portal):
    application, audit = ApplicationTrackerAgent(portal).submit(make_application())
    assert application.status is ApplicationStatus.SUBMITTED
    assert application.reference_number
    assert application.submitted_at is not None
    assert "submitted" in audit.action


def test_tracker_refuses_a_form_with_flagged_fields(portal):
    """A mechanical guard behind the human gate. Both must pass."""
    application = make_application(form_data={"annual_income": FLAG})
    with pytest.raises(SubmissionRefused, match="need a human"):
        ApplicationTrackerAgent(portal).submit(application)
    assert portal.submissions == []


def test_tracker_refuses_when_documents_are_missing(portal, household, corpus):
    from yogya.models.application import DocumentGap

    application = make_application(
        document_gaps=[
            DocumentGap(
                document=DocumentKind.DEATH_CERTIFICATE,
                required_for="x",
                where_to_get="registrar",
                blocking=True,
            )
        ]
    )
    with pytest.raises(SubmissionRefused, match="blocking documents"):
        ApplicationTrackerAgent(portal).submit(application)


def test_poll_records_approval(portal):
    tracker = ApplicationTrackerAgent(portal)
    application, _ = tracker.submit(make_application())
    application, audit = tracker.poll(application)
    assert application.status is ApplicationStatus.APPROVED
    assert application.decided_at is not None


def test_poll_records_a_rejection_with_its_reason():
    portal = SimulatedPortal()
    portal.script("app-1", rejected("Documents incomplete.", code="DOCS"))
    tracker = ApplicationTrackerAgent(portal)
    application, _ = tracker.submit(make_application())
    application, _ = tracker.poll(application)
    assert application.status is ApplicationStatus.REJECTED
    assert application.rejection.code == "DOCS"


def test_poll_after_an_appeal_asks_a_different_question():
    """An appeal is not a retry of the same request; the portal may answer it
    differently, and the attempt number is how it knows."""
    portal = SimulatedPortal()
    portal.script("app-1", rejected("no"), approved("REF-OK"))
    tracker = ApplicationTrackerAgent(portal)
    application, _ = tracker.submit(make_application())
    application, _ = tracker.poll(application)
    assert application.status is ApplicationStatus.REJECTED

    application.appeal_attempts = 1
    application, _ = tracker.poll(application)
    assert application.status is ApplicationStatus.APPROVED


def test_overdue_detection_uses_the_service_standard():
    from datetime import datetime, timedelta, timezone

    application = make_application(status=ApplicationStatus.WAITING)
    application.submitted_at = datetime.now(timezone.utc) - timedelta(days=45)
    assert ApplicationTrackerAgent.is_overdue(application, service_standard_days=30)
    assert not ApplicationTrackerAgent.is_overdue(application, service_standard_days=90)


def test_a_decided_application_is_never_overdue():
    application = make_application(status=ApplicationStatus.APPROVED)
    assert not ApplicationTrackerAgent.is_overdue(application)


# ══ 6. Grievance Escalation ════════════════════════════════════════════════


def test_rejection_contradicted_by_the_rules_is_wrongful(llm, household, corpus):
    """The family is eligible on the department's own published criteria and
    was refused anyway. That contradiction — not a model's opinion — is what
    makes the denial wrongful."""
    agent = GrievanceEscalationAgent(llm, corpus)
    application = make_application(
        scheme_id="nfsa-phh", scheme_name="NFSA", rule_version="1.0.0"
    )
    application.rejection = RejectionReason(code="BPL", text="Not below poverty line.")

    application, audit, wrongful = agent.assess(household, application)
    assert wrongful is True
    assert application.rejection.assessed_as_wrongful is True
    assert "wrongful" in audit.action
    assert audit.citations


def test_rejection_consistent_with_the_rules_is_defensible(llm, household, corpus):
    household.ration_card = RationCardType.APL  # genuinely not eligible now
    agent = GrievanceEscalationAgent(llm, corpus)
    application = make_application(
        scheme_id="nfsa-phh", scheme_name="NFSA", rule_version="1.0.0"
    )
    application.rejection = RejectionReason(code="BPL", text="Not below poverty line.")

    _, audit, wrongful = agent.assess(household, application)
    assert wrongful is False
    assert "defensible" in audit.action


def test_the_model_cannot_manufacture_a_wrongful_denial(household, corpus):
    """A model that insists every refusal is an outrage must not be able to
    put a family through a pointless appeal."""
    from yogya.llm.fake import FakeLLM

    llm = FakeLLM()
    llm.script(
        "AppealAssessmentOutput",
        lambda s, u: {
            "assessed_as_wrongful": True,
            "grounds": "Outrageous!",
            "confidence": 1.0,
        },
    )
    household.ration_card = RationCardType.APL
    application = make_application(scheme_id="nfsa-phh", rule_version="1.0.0")
    application.rejection = RejectionReason(code="BPL", text="Not BPL.")

    _, _, wrongful = GrievanceEscalationAgent(llm, corpus).assess(household, application)
    assert wrongful is False


def test_assessment_uses_the_rule_version_the_case_was_filed_under(llm, household, corpus):
    """Re-checking a 2023 rejection against 2024's looser rules would invent a
    wrongful denial. The application's own rule_version governs."""
    agent = GrievanceEscalationAgent(llm, corpus)
    household.annual_income = 50_000  # over the old ceiling, under the new one
    application = make_application(
        scheme_id="up-old-age-pension", scheme_name="UP OAP", rule_version="1.0.0"
    )
    application.rejection = RejectionReason(code="INCOME", text="Income too high.")

    _, audit, wrongful = agent.assess(household, application)
    assert audit.rule_version == "1.0.0"
    assert wrongful is False  # correct under the rules in force at the time


def test_appeal_letter_cites_only_satisfied_criteria(llm, household, corpus):
    agent = GrievanceEscalationAgent(llm, corpus)
    application = make_application(scheme_id="nfsa-phh", rule_version="1.0.0")
    application.rejection = RejectionReason(code="BPL", text="Not BPL.")
    agent.assess(household, application)

    appeal, audit = agent.draft_appeal(household, application)
    assert appeal.attempt == 1
    assert appeal.citations
    assert appeal.letter_text.strip()
    assert audit.citations == sorted(set(appeal.citations))


def test_template_letter_is_used_when_the_model_is_unavailable(household, corpus):
    class Exploding:
        def structured(self, **kwargs):
            raise RuntimeError("down")

        def text(self, **kwargs):
            raise RuntimeError("down")

    agent = GrievanceEscalationAgent(Exploding(), corpus)
    application = make_application(scheme_id="nfsa-phh", rule_version="1.0.0")
    application.rejection = RejectionReason(code="BPL", text="Not BPL.")
    agent.assess(household, application)

    appeal, _ = agent.draft_appeal(household, application)
    assert "Appellate Authority" in appeal.letter_text
    assert "Test Devi" in appeal.letter_text


def test_recording_an_appeal_advances_the_attempt_counter(llm, household, corpus):
    agent = GrievanceEscalationAgent(llm, corpus)
    application = make_application(scheme_id="nfsa-phh", rule_version="1.0.0")
    application.rejection = RejectionReason(code="BPL", text="Not BPL.")
    agent.assess(household, application)
    appeal, _ = agent.draft_appeal(household, application)

    application = agent.record_appeal_filed(application, appeal)
    assert application.appeal_attempts == 1
    assert application.status is ApplicationStatus.APPEALED
    assert application.appeals[0].filed_at is not None


def test_escalation_names_the_grievance_channel(llm, household, corpus):
    agent = GrievanceEscalationAgent(llm, corpus)
    application = make_application(scheme_id="nfsa-phh", rule_version="1.0.0")
    application, audit = agent.escalate(household, application, reason="exhausted")
    assert application.status is ApplicationStatus.ESCALATED
    assert "grievance" in audit.action
    assert "nfsa.gov.in" in audit.detail


def test_assessing_without_a_rejection_is_a_programming_error(llm, household, corpus):
    with pytest.raises(ValueError, match="no rejection"):
        GrievanceEscalationAgent(llm, corpus).assess(household, make_application())
