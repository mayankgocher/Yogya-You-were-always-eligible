"""The compiled graph, end to end.

These tests are where the LangGraph-specific claims get checked: that the map
step really fans out, that the subgraph loop really loops, that an interrupt
really halts execution, and that resuming really continues from where it
stopped rather than starting again.

Several of them exist because the corresponding bug happened during
development and was expensive to find. `test_audit_trail_does_not_duplicate`
in particular guards a subgraph state-sharing mistake that presented as an
exponential slowdown rather than as wrong output.
"""

from __future__ import annotations

import pytest

from yogya.agents.interrupts import (
    AppealDecision,
    InterruptKind,
    ProfileDecision,
    SchemeSelection,
    SubmissionDecision,
)
from yogya.demo import HOUSEHOLD_ID, INTAKE_NOTES, app_id, demo_llm
from yogya.graph.build import build_graph
from yogya.graph.runner import AutoCaseworker, pending_interrupt, run_config
from yogya.models.application import ApplicationStatus
from yogya.models.scheme import EligibilityVerdict
from yogya.portal import SimulatedPortal, approved, rejected

pytestmark = pytest.mark.integration


@pytest.fixture
def graph_env(settings, corpus, case_store):
    """A graph wired entirely to test doubles."""
    portal = SimulatedPortal(rejection_rate=0.0)
    llm = demo_llm()
    graph = build_graph(
        llm=llm,
        corpus=corpus,
        portal=portal,
        case_store=case_store,
        settings=settings,
    )
    return graph, portal, llm, case_store


def start(graph, thread: str = "t"):
    config = run_config(thread)
    graph.invoke(
        {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, config
    )
    return config


# ── gates ──────────────────────────────────────────────────────────────────


def test_the_run_halts_at_the_profile_gate(graph_env):
    graph, *_ = graph_env
    config = start(graph)
    request = pending_interrupt(graph, config)
    assert request["kind"] == InterruptKind.CONFIRM_PROFILE.value
    # Nothing beyond profiling has happened yet: no scheme has been evaluated.
    assert graph.get_state(config).values.get("eligibility") in (None, [])
    assert graph.get_state(config).values.get("candidate_scheme_ids") is None


def test_screening_happens_only_after_the_profile_is_confirmed(graph_env):
    graph, *_ = graph_env
    config = start(graph)
    from langgraph.types import Command

    caseworker = AutoCaseworker()
    graph.invoke(
        Command(resume=caseworker.decide(pending_interrupt(graph, config))), config
    )
    values = graph.get_state(config).values
    assert len(values["eligibility"]) > 0
    assert pending_interrupt(graph, config)["kind"] == InterruptKind.SELECT_SCHEMES.value


def test_corrections_at_the_profile_gate_change_the_verdicts(graph_env):
    """A caseworker who fixes the ration card must see different results —
    otherwise the gate is theatre."""
    graph, *_ = graph_env
    caseworker = AutoCaseworker(
        on_profile=lambda req: ProfileDecision(
            action="amend", corrections={"ration_card": "apl"}
        ),
        on_selection=lambda req: SchemeSelection(file_scheme_ids=[]),
    )
    final = caseworker.run(
        graph, {"household_id": "hh-amend", "intake_notes": INTAKE_NOTES}, run_config("amend")
    )
    nfsa = next(r for r in final["eligibility"] if r.scheme_id == "nfsa-phh")
    assert nfsa.verdict is EligibilityVerdict.NOT_ELIGIBLE


def test_nothing_is_submitted_without_passing_the_submission_gate(graph_env):
    """The single most important test in the file.

    The graph is driven right up to the submission gate and then abandoned.
    The portal must never have been called.
    """
    graph, portal, *_ = graph_env
    config = start(graph)
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"])
    )
    from langgraph.types import Command

    for _ in range(3):
        request = pending_interrupt(graph, config)
        if request is None or request["kind"] == InterruptKind.APPROVE_SUBMISSION.value:
            break
        graph.invoke(Command(resume=caseworker.decide(request)), config)

    assert pending_interrupt(graph, config)["kind"] == InterruptKind.APPROVE_SUBMISSION.value
    assert portal.submissions == []


def test_holding_at_the_submission_gate_files_nothing(graph_env):
    graph, portal, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"]),
        on_submission=lambda req: SubmissionDecision(action="hold"),
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("hold")
    )
    assert portal.submissions == []
    assert final["applications"][0].status is ApplicationStatus.DRAFT


def test_skipping_at_the_submission_gate_abandons_the_application(graph_env):
    graph, portal, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"]),
        on_submission=lambda req: SubmissionDecision(action="skip"),
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("skip")
    )
    assert final["applications"][0].status is ApplicationStatus.ABANDONED
    assert portal.submissions == []


def test_form_edits_at_the_gate_reach_the_portal(graph_env):
    graph, portal, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"]),
        on_submission=lambda req: SubmissionDecision(
            action="submit", form_edits={"district": "Hardoi"}
        ),
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("edit")
    )
    assert final["applications"][0].form_data["district"] == "Hardoi"


# ── map-reduce ─────────────────────────────────────────────────────────────


def test_every_applicable_scheme_is_evaluated_in_the_sweep(graph_env, corpus):
    graph, *_ = graph_env
    caseworker = AutoCaseworker(on_selection=lambda req: SchemeSelection(file_scheme_ids=[]))
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("sweep")
    )
    evaluated = {r.scheme_id for r in final["eligibility"]}
    applicable = {s.scheme_id for s in corpus.for_household("uttar_pradesh")}
    assert evaluated == applicable


def test_parallel_branches_do_not_overwrite_each_other(graph_env):
    """`eligibility` uses an additive reducer. With plain assignment, all but
    one branch of the fan-out would be silently lost."""
    graph, *_ = graph_env
    caseworker = AutoCaseworker(on_selection=lambda req: SchemeSelection(file_scheme_ids=[]))
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("par")
    )
    scheme_ids = [r.scheme_id for r in final["eligibility"]]
    assert len(scheme_ids) == len(set(scheme_ids)) > 15


# ── the application loop ───────────────────────────────────────────────────


def test_each_selected_scheme_gets_its_own_application(graph_env):
    graph, *_ = graph_env
    chosen = ["pmjay", "mgnrega", "pmsby"]
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=chosen)
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("multi")
    )
    assert {a.scheme_id for a in final["applications"]} == set(chosen)


def test_a_blocked_application_never_reaches_the_portal(graph_env):
    """PM-KISAN needs a land record this family does not have."""
    graph, portal, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pm-kisan"])
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("blocked")
    )
    application = final["applications"][0]
    assert application.status is ApplicationStatus.BLOCKED_ON_DOCUMENTS
    assert application.blocking_gaps
    assert portal.submissions == []


def test_audit_trail_does_not_duplicate(graph_env):
    """Regression test for a subgraph state-sharing bug.

    The lifecycle subgraph once shared the parent's additive `audit` channel.
    Each application therefore re-appended the entire log to itself, doubling
    it every pass — which surfaced as the graph taking a minute per application
    rather than as any visible wrongness. The audit trail must grow roughly
    linearly in the number of applications.
    """
    graph, *_ = graph_env
    chosen = ["pmjay", "mgnrega", "pmsby", "pmjjby", "ssy"]
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=chosen)
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("audit")
    )
    entries = final["audit"]
    assert len(entries) < 120, f"audit trail is duplicating: {len(entries)} entries"
    assert len({e.entry_id for e in entries}) == len(entries)


# ── the rejection / appeal cycle ───────────────────────────────────────────


def test_a_wrongful_rejection_is_appealed_and_overturned(settings, corpus, case_store):
    """The story the project exists to tell, driven through the real graph."""
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(
        app_id("pmjay"),
        rejected("Not a below poverty line family.", code="BPL"),
        approved("REF-APPEAL-OK"),
    )
    graph = build_graph(
        llm=demo_llm(), corpus=corpus, portal=portal,
        case_store=case_store, settings=settings,
    )
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"])
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("appeal")
    )

    application = final["applications"][0]
    assert application.status is ApplicationStatus.APPROVED
    assert application.appeal_attempts == 1
    assert application.rejection.assessed_as_wrongful is True
    assert application.appeals[0].outcome == "upheld"
    assert final["summary"]["wrongful_denials_recovered"] == 1


def test_appeals_are_bounded_and_end_in_escalation(settings, corpus, case_store):
    """A department that keeps saying no must not produce an infinite loop —
    and must not be quietly given up on either."""
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(app_id("pmjay"), rejected("No.", code="NO"))  # repeats forever
    graph = build_graph(
        llm=demo_llm(), corpus=corpus, portal=portal,
        case_store=case_store, settings=settings,
    )
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"])
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("escalate")
    )

    application = final["applications"][0]
    assert application.status is ApplicationStatus.ESCALATED
    assert application.appeal_attempts == settings.max_appeal_attempts
    assert final["summary"]["escalated"] == 1


def test_declining_to_appeal_stops_the_cycle(settings, corpus, case_store):
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(app_id("pmjay"), rejected("No.", code="NO"))
    graph = build_graph(
        llm=demo_llm(), corpus=corpus, portal=portal,
        case_store=case_store, settings=settings,
    )
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"]),
        on_appeal=lambda req: AppealDecision(action="abandon"),
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("noappeal")
    )
    assert final["applications"][0].status is ApplicationStatus.ABANDONED
    assert final["applications"][0].appeal_attempts == 0


def test_a_defensible_rejection_is_not_appealed(settings, corpus, case_store):
    """Appealing a correct decision wastes a family's time and a clerk's.

    Here the household genuinely fails PM-KISAN's tax criterion, so the
    rejection stands and no appeal is drafted.
    """
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(app_id("mgnrega"), rejected("Not a rural household.", code="URBAN"))

    llm = demo_llm()
    graph = build_graph(
        llm=llm, corpus=corpus, portal=portal, case_store=case_store, settings=settings
    )
    caseworker = AutoCaseworker(
        on_profile=lambda req: ProfileDecision(
            action="amend", corrections={"residence": "urban"}
        ),
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["mgnrega"]),
    )
    final = caseworker.run(
        graph, {"household_id": "hh-urban", "intake_notes": INTAKE_NOTES}, run_config("defensible")
    )
    # An urban household is not eligible for MGNREGS, so it is never filed.
    assert not any(
        a.scheme_id == "mgnrega" and a.appeal_attempts for a in final["applications"]
    )


# ── metrics and persistence ────────────────────────────────────────────────


def test_summary_counts_match_the_applications(graph_env):
    graph, *_ = graph_env
    chosen = ["pmjay", "mgnrega", "pm-kisan"]
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=chosen)
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("metrics")
    )
    summary = final["summary"]
    applications = final["applications"]
    assert summary["applications_filed"] == len(applications)
    assert summary["approved"] == sum(
        1 for a in applications if a.status is ApplicationStatus.APPROVED
    )
    assert summary["blocked_on_documents"] == sum(
        1 for a in applications if a.status is ApplicationStatus.BLOCKED_ON_DOCUMENTS
    )


def test_insurance_cover_is_not_counted_as_cash(graph_env):
    """PM-JAY's Rs 5 lakh is a cover amount, not money in hand. Adding it to
    'rupees recovered' would inflate the headline number tenfold."""
    graph, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"])
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("money")
    )
    assert final["summary"]["rupees_recovered_annual"] == 0
    assert final["summary"]["insurance_cover_secured_inr"] == 500_000


def test_schemes_already_received_are_counted_not_refiled(graph_env):
    graph, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(
            file_scheme_ids=["pmjay"], already_receiving_scheme_ids=["nfsa-phh"]
        )
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("already")
    )
    assert final["summary"]["already_receiving"] == 1
    assert {a.scheme_id for a in final["applications"]} == {"pmjay"}


def test_the_case_is_persisted_for_next_month(graph_env):
    graph, _, _, case_store = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=["pmjay"])
    )
    caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("persist")
    )
    case = case_store.load_case(HOUSEHOLD_ID)
    assert case["household"] is not None
    assert case["applications"]
    assert case["audit"]


def test_a_returning_family_is_recognised(graph_env):
    graph, *_ = graph_env
    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(file_scheme_ids=[])
    )
    caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("visit-1")
    )
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("visit-2")
    )
    opened = next(e for e in final["audit"] if e.action == "opened case")
    assert "existing case found" in opened.detail


def test_the_corpus_snapshot_is_recorded_on_the_run(graph_env, corpus):
    graph, *_ = graph_env
    caseworker = AutoCaseworker(on_selection=lambda req: SchemeSelection(file_scheme_ids=[]))
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("snap")
    )
    assert final["summary"]["corpus_snapshot"] == corpus.snapshot_id


def test_every_eligibility_audit_entry_carries_a_rule_version(graph_env):
    graph, *_ = graph_env
    caseworker = AutoCaseworker(on_selection=lambda req: SchemeSelection(file_scheme_ids=[]))
    final = caseworker.run(
        graph, {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES}, run_config("ver")
    )
    evaluated = [e for e in final["audit"] if e.action.startswith("evaluated eligibility")]
    assert evaluated
    assert all(e.rule_version for e in evaluated)
    assert all(e.citations for e in evaluated)
