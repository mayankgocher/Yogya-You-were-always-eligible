"""The application lifecycle subgraph in isolation.

Testing the subgraph directly, rather than only through the outer graph, is
worth the extra file: it makes the routing table explicit and it means a
broken edge fails with a two-node trace rather than a forty-node one.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from yogya.agents.interrupts import InterruptKind
from yogya.demo import demo_llm
from yogya.graph.build import default_serializer
from yogya.graph.lifecycle import build_lifecycle_graph
from yogya.graph.runner import pending_interrupt
from yogya.models.application import Application, ApplicationStatus
from yogya.portal import SimulatedPortal, approved, rejected

pytestmark = pytest.mark.integration


@pytest.fixture
def lifecycle(settings, corpus):
    def build(portal):
        return build_lifecycle_graph(
            llm=demo_llm(),
            corpus=corpus,
            portal=portal,
            settings=settings,
            # Standalone runs need their own checkpointer; inside the outer
            # graph the subgraph inherits the parent's.
            checkpointer=InMemorySaver(serde=default_serializer()),
        )

    return build


def application_for(scheme_id: str, corpus, household_id="hh-test") -> Application:
    scheme = corpus.get(scheme_id)
    return Application(
        application_id=f"app-{household_id}-{scheme_id}",
        household_id=household_id,
        scheme_id=scheme_id,
        scheme_name=scheme.name,
        rule_version=scheme.rule_version,
        estimated_annual_value_inr=scheme.benefit_value_annual_inr,
    )


def run(graph, household, application, thread, answers):
    """Drive the subgraph, answering each gate from `answers` by kind."""
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 60}
    graph.invoke({"household": household, "application": application}, config)
    for _ in range(20):
        request = pending_interrupt(graph, config)
        if request is None:
            break
        graph.invoke(Command(resume=answers[request["kind"]]), config)
    return graph.get_state(config).values


# ── routing ────────────────────────────────────────────────────────────────


def test_a_blocked_application_ends_before_the_form_is_filled(
    lifecycle, household, corpus, portal
):
    """PM-KISAN needs a land record. There is no point drafting a form, and no
    point asking the caseworker to approve one."""
    graph = lifecycle(portal)
    config = {"configurable": {"thread_id": "blocked"}, "recursion_limit": 60}
    graph.invoke(
        {"household": household, "application": application_for("pm-kisan", corpus)},
        config,
    )
    values = graph.get_state(config).values
    assert values["application"].status is ApplicationStatus.BLOCKED_ON_DOCUMENTS
    assert pending_interrupt(graph, config) is None
    assert values["application"].form_data == {}


def test_a_complete_application_stops_at_the_submission_gate(
    lifecycle, household, corpus, portal
):
    graph = lifecycle(portal)
    config = {"configurable": {"thread_id": "gate"}, "recursion_limit": 60}
    graph.invoke(
        {"household": household, "application": application_for("pmjay", corpus)},
        config,
    )
    request = pending_interrupt(graph, config)
    assert request["kind"] == InterruptKind.APPROVE_SUBMISSION.value
    assert request["payload"]["application"]["form_data"]
    assert portal.submissions == []


def test_approval_leads_to_submission_and_a_decision(
    lifecycle, household, corpus, portal
):
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "approve",
        {InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"}},
    )
    assert values["application"].status is ApplicationStatus.APPROVED
    assert len(portal.submissions) == 1


def test_the_appeal_cycle_returns_to_polling(lifecycle, household, corpus):
    """poll → assess → draft → approve → poll. The cycle that makes this a
    graph rather than a pipeline."""
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(
        "app-hh-test-pmjay",
        rejected("Not BPL.", code="BPL"),
        approved("REF-2"),
    )
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "cycle",
        {
            InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"},
            InterruptKind.APPROVE_APPEAL.value: {"action": "file"},
        },
    )
    application = values["application"]
    assert application.status is ApplicationStatus.APPROVED
    assert application.appeal_attempts == 1
    # Polled twice: once for the original decision, once after the appeal.
    assert [attempt for _, attempt in portal.polls] == [0, 1]


def test_the_cycle_terminates_at_the_appeal_limit(lifecycle, household, corpus, settings):
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script("app-hh-test-pmjay", rejected("No.", code="NO"))
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "limit",
        {
            InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"},
            InterruptKind.APPROVE_APPEAL.value: {"action": "file"},
        },
    )
    assert values["application"].status is ApplicationStatus.ESCALATED
    assert values["application"].appeal_attempts == settings.max_appeal_attempts


def test_a_still_pending_application_ends_the_run_without_escalating(
    lifecycle, household, corpus
):
    """Waiting is not rejection. The run ends; the case stays open."""
    from yogya.portal import still_waiting

    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script("app-hh-test-pmjay", still_waiting())
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "waiting",
        {InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"}},
    )
    assert values["application"].status is ApplicationStatus.WAITING
    assert values["application"].appeal_attempts == 0


# ── state hygiene ──────────────────────────────────────────────────────────


def test_the_subgraph_accumulates_into_its_own_channels(
    lifecycle, household, corpus, portal
):
    """The subgraph must not write to the parent's `audit`/`metrics` keys.

    Sharing those channels caused the audit trail to double on every
    application. The names below are load-bearing — see
    `yogya.models.state.ApplicationState`.
    """
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "channels",
        {InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"}},
    )
    assert "run_audit" in values
    assert "run_metrics" in values
    assert "audit" not in values
    assert "metrics" not in values


def test_metrics_are_counted_once(lifecycle, household, corpus, portal):
    graph = lifecycle(portal)
    values = run(
        graph,
        household,
        application_for("pmjay", corpus),
        "metrics",
        {InterruptKind.APPROVE_SUBMISSION.value: {"action": "submit"}},
    )
    metrics = values["run_metrics"]
    assert metrics.applications_approved == 1
    assert metrics.human_interrupts == 1
    assert metrics.form_fields_total > 0


def test_state_survives_a_serialisation_round_trip(lifecycle, household, corpus, portal):
    """Every model that travels through the graph must be declared to the
    checkpoint serialiser, or it comes back as a dict and breaks far away."""
    graph = build_lifecycle_graph(
        llm=demo_llm(),
        corpus=corpus,
        portal=portal,
        settings=__import__("yogya.config", fromlist=["Settings"]).Settings(
            llm_offline=True
        ),
    )
    saver = InMemorySaver(serde=default_serializer())
    config = {"configurable": {"thread_id": "serde"}, "recursion_limit": 60}
    compiled = graph
    # Round-trip the models the subgraph puts in state.
    application = application_for("pmjay", corpus)
    payload = {"household": household, "application": application}
    restored = saver.serde.loads_typed(saver.serde.dumps_typed(payload))
    assert restored["household"].head_name == household.head_name
    assert restored["application"].scheme_id == "pmjay"
    assert isinstance(restored["application"].status, ApplicationStatus)
