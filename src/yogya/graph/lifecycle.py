"""Application lifecycle subgraph.

One compiled subgraph, instantiated once and reused as a node in the outer
graph for each application in turn.

The shape that matters:

    check_documents ──blocked──────────────────────────────► END
          │
       fill_form ──► approve_submission ──hold/skip───────► END
                            │ submit
                         submit ──► poll ──approved───────► END
                                      │
                                   rejected
                                      │
                              assess_rejection ──defensible► END
                                      │ wrongful
                          ┌───────────┴────────────┐
                    attempts left            attempts spent
                          │                         │
                    draft_appeal                escalate ──► END
                          │
                  approve_appeal ──abandon───────────────► END
                          │ file
                     file_appeal ──────► poll   (the loop)

The cycle poll → assess → appeal → poll is the whole reason this is a graph and
not a pipeline, and `max_appeal_attempts` is what stops it being an infinite
one. Two interrupts sit on the cycle, so a rejection can never become an appeal
without a human reading the letter.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from yogya.agents.documents import DocumentGapAgent
from yogya.agents.forms import FormFillerAgent
from yogya.agents.grievance import GrievanceEscalationAgent
from yogya.agents.interrupts import (
    AppealDecision,
    SubmissionDecision,
    approve_appeal_request,
    approve_submission_request,
)
from yogya.agents.tracker import ApplicationTrackerAgent, SubmissionRefused
from yogya.audit import entry
from yogya.config import Settings
from yogya.data.loader import SchemeCorpus
from yogya.models.application import ApplicationStatus, RunMetrics
from yogya.models.state import ApplicationState
from yogya.portal import PortalGateway


def build_lifecycle_graph(
    *,
    llm,
    corpus: SchemeCorpus,
    portal: PortalGateway,
    settings: Settings,
    checkpointer=None,
):
    """Compile the per-application subgraph.

    `checkpointer` is normally left None: as a node inside the outer graph the
    subgraph inherits the parent's checkpointer, and giving it one of its own
    would fight that. Pass one only to run this subgraph standalone — which the
    tests do, so a routing bug fails with a two-node trace instead of a
    forty-node one.
    """
    documents = DocumentGapAgent(llm)
    forms = FormFillerAgent(llm)
    tracker = ApplicationTrackerAgent(portal)
    grievance = GrievanceEscalationAgent(llm, corpus)

    # ---- nodes ------------------------------------------------------------
    def check_documents(state: ApplicationState) -> dict:
        household = state["household"]
        application = state["application"]
        scheme = corpus.get(application.scheme_id, version=application.rule_version)

        gaps, audit = documents.run(
            household, scheme, application_id=application.application_id
        )
        application.document_gaps = gaps
        application.attached_documents = sorted(
            household.available_documents, key=lambda d: d.value
        )

        if application.blocking_gaps:
            application.status = ApplicationStatus.BLOCKED_ON_DOCUMENTS
        application.touch()
        return {"application": application, "run_audit": [audit]}

    def fill_form(state: ApplicationState) -> dict:
        household = state["household"]
        application = state["application"]
        scheme = corpus.get(application.scheme_id, version=application.rule_version)

        form, flagged, audit = forms.run(
            household, scheme, application_id=application.application_id
        )
        application.form_data = form
        application.status = ApplicationStatus.AWAITING_APPROVAL
        application.touch()

        return {
            "application": application,
            "run_audit": [audit],
            "run_metrics": RunMetrics(
                form_fields_total=len(form), form_fields_flagged=len(flagged)
            ),
        }

    def approve_submission(state: ApplicationState) -> dict:
        """The mandatory human gate. Nothing reaches a portal above this line."""
        application = state["application"]
        scheme = corpus.get(application.scheme_id, version=application.rule_version)

        raw = interrupt(
            approve_submission_request(application, scheme.name).model_dump(mode="json")
        )
        decision = _coerce(raw, SubmissionDecision)

        if decision.form_edits:
            application.form_data.update(decision.form_edits)
        application.touch()

        audit = entry(
            household_id=application.household_id,
            actor="social_worker",
            action=f"submission decision: {decision.action}",
            detail=(
                f"{scheme.name}"
                + (f"; edited {len(decision.form_edits)} field(s)" if decision.form_edits else "")
                + (f"; note: {decision.note}" if decision.note else "")
            ),
            scheme_id=application.scheme_id,
            application_id=application.application_id,
        )

        if decision.action == "skip":
            application.status = ApplicationStatus.ABANDONED
        elif decision.action == "hold":
            application.status = ApplicationStatus.DRAFT

        return {
            "application": application,
            "run_audit": [audit],
            "run_metrics": RunMetrics(human_interrupts=1),
            "portal_outcome": {"decision": decision.action},
        }

    def submit(state: ApplicationState) -> dict:
        application = state["application"]
        try:
            application, audit = tracker.submit(application)
        except SubmissionRefused as exc:
            application.status = ApplicationStatus.BLOCKED_ON_DOCUMENTS
            application.touch()
            audit = entry(
                household_id=application.household_id,
                actor="application_tracker",
                action="refused to submit",
                detail=str(exc),
                scheme_id=application.scheme_id,
                application_id=application.application_id,
            )
        return {"application": application, "run_audit": [audit]}

    def poll(state: ApplicationState) -> dict:
        application, audit = tracker.poll(state["application"])
        metrics = RunMetrics()
        if application.status is ApplicationStatus.APPROVED:
            metrics.applications_approved = 1
            metrics.rupees_recovered_annual = (
                application.estimated_annual_value_inr or 0
            )
            if application.appeal_attempts:
                metrics.appeals_upheld = 1
                if application.appeals:
                    application.appeals[-1].outcome = "upheld"
        elif application.status is ApplicationStatus.REJECTED:
            metrics.applications_rejected = 1
            if application.appeals:
                application.appeals[-1].outcome = "dismissed"
        return {"application": application, "run_audit": [audit], "run_metrics": metrics}

    def assess_rejection(state: ApplicationState) -> dict:
        application, audit, _ = grievance.assess(state["household"], state["application"])
        return {"application": application, "run_audit": [audit]}

    def draft_appeal(state: ApplicationState) -> dict:
        appeal, audit = grievance.draft_appeal(state["household"], state["application"])
        return {
            "application": state["application"],
            "run_audit": [audit],
            "portal_outcome": {"draft_appeal": appeal.model_dump(mode="json")},
        }

    def approve_appeal(state: ApplicationState) -> dict:
        from yogya.models.application import Appeal

        application = state["application"]
        scheme = corpus.get(application.scheme_id, version=application.rule_version)
        appeal = Appeal.model_validate(state["portal_outcome"]["draft_appeal"])

        raw = interrupt(
            approve_appeal_request(application, appeal, scheme.name).model_dump(
                mode="json"
            )
        )
        decision = _coerce(raw, AppealDecision)

        metrics = RunMetrics(human_interrupts=1)
        if decision.action == "abandon":
            application.status = ApplicationStatus.ABANDONED
            action_detail = "declined to appeal"
        else:
            if decision.action == "edit_and_file" and decision.letter_text:
                appeal.letter_text = decision.letter_text
            application = grievance.record_appeal_filed(application, appeal)
            metrics.appeals_filed = 1
            action_detail = f"filed appeal attempt {appeal.attempt}"

        audit = entry(
            household_id=application.household_id,
            actor="social_worker",
            action=f"appeal decision: {decision.action}",
            detail=f"{scheme.name}: {action_detail}"
            + (f"; note: {decision.note}" if decision.note else ""),
            scheme_id=application.scheme_id,
            application_id=application.application_id,
            citations=appeal.citations,
        )
        return {"application": application, "run_audit": [audit], "run_metrics": metrics}

    def escalate(state: ApplicationState) -> dict:
        application, audit = grievance.escalate(
            state["household"],
            state["application"],
            reason="appeal attempts exhausted without relief",
        )
        return {
            "application": application,
            "run_audit": [audit],
            "run_metrics": RunMetrics(grievances_escalated=1),
        }

    # ---- edges ------------------------------------------------------------
    def after_documents(state: ApplicationState) -> str:
        return "blocked" if state["application"].blocking_gaps else "proceed"

    def after_approval(state: ApplicationState) -> str:
        return (
            "submit"
            if state.get("portal_outcome", {}).get("decision") == "submit"
            else "stop"
        )

    def after_submit(state: ApplicationState) -> str:
        application = state["application"]
        return "poll" if application.status is ApplicationStatus.SUBMITTED else "stop"

    def after_poll(state: ApplicationState) -> str:
        status = state["application"].status
        if status is ApplicationStatus.REJECTED:
            return "assess"
        return "stop"  # approved, or still waiting — both end this run

    def after_assessment(state: ApplicationState) -> str:
        application = state["application"]
        wrongful = bool(
            application.rejection and application.rejection.assessed_as_wrongful
        )
        if not wrongful:
            return "stop"
        if application.appeal_attempts >= settings.max_appeal_attempts:
            return "escalate"
        return "appeal"

    def after_appeal_decision(state: ApplicationState) -> str:
        return (
            "stop"
            if state["application"].status is ApplicationStatus.ABANDONED
            else "poll"
        )

    graph = StateGraph(ApplicationState)
    graph.add_node("check_documents", check_documents)
    graph.add_node("fill_form", fill_form)
    graph.add_node("approve_submission", approve_submission)
    graph.add_node("submit", submit)
    graph.add_node("poll", poll)
    graph.add_node("assess_rejection", assess_rejection)
    graph.add_node("draft_appeal", draft_appeal)
    graph.add_node("approve_appeal", approve_appeal)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "check_documents")
    graph.add_conditional_edges(
        "check_documents", after_documents, {"blocked": END, "proceed": "fill_form"}
    )
    graph.add_edge("fill_form", "approve_submission")
    graph.add_conditional_edges(
        "approve_submission", after_approval, {"submit": "submit", "stop": END}
    )
    graph.add_conditional_edges("submit", after_submit, {"poll": "poll", "stop": END})
    graph.add_conditional_edges(
        "poll", after_poll, {"assess": "assess_rejection", "stop": END}
    )
    graph.add_conditional_edges(
        "assess_rejection",
        after_assessment,
        {"appeal": "draft_appeal", "escalate": "escalate", "stop": END},
    )
    graph.add_edge("draft_appeal", "approve_appeal")
    graph.add_conditional_edges(
        "approve_appeal", after_appeal_decision, {"poll": "poll", "stop": END}
    )
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer)


def _coerce(raw, model):
    """Accept a dict, a model instance, or a bare action string from resume."""
    if isinstance(raw, model):
        return raw
    if isinstance(raw, str):
        return model(action=raw)
    if isinstance(raw, dict):
        return model.model_validate(raw)
    return model()


__all__ = ["build_lifecycle_graph", "Command"]
