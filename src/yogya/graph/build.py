"""The outer graph: one household, one screening session.

    load_case ─► profile ─► confirm_profile ─► retrieve
                                                  │
                                    ┌─────────────┴──────────────┐
                                    │  Send(one per scheme)      │   ← map
                                    ▼                            ▼
                              check_eligibility           check_eligibility
                                    └─────────────┬──────────────┘
                                                  ▼                  ← reduce
                                            collect_results
                                                  │
                                          select_schemes  (interrupt)
                                                  │
                                        prepare_applications
                                                  │
                                          ┌──► next_application ──empty──► summarise ─► persist ─► END
                                          │            │
                                          │      application_lifecycle   (subgraph)
                                          │            │
                                          └──────── archive

Two design notes worth reading before changing anything here.

**Why eligibility fans out and applications do not.** Eligibility checks are
pure, fast and interrupt-free, so `Send` gives real parallelism for free. The
application lifecycle contains human interrupts; running twelve of those in
parallel would raise twelve simultaneous interrupts and force the caller to
resume them by id. That is worse for the only user who matters — a social
worker approves one form at a time — so applications run through the subgraph
sequentially via a queue and a loop edge. The parallelism is where it helps and
absent where it would only look impressive.

**Why the subgraph is a real subgraph.** It is compiled once and added as a
node, sharing state keys with the parent, rather than invoked inside a Python
loop. That keeps checkpointing and interrupt-resume working correctly across
the loop, which a hand-rolled `for` loop inside a node would break.
"""

from __future__ import annotations


from datetime import date

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from yogya.agents.interrupts import (
    ProfileDecision,
    SchemeSelection,
    confirm_profile_request,
    select_schemes_request,
)
from yogya.agents.matcher import EntitlementMatcher
from yogya.agents.profiler import HouseholdProfiler
from yogya.audit import entry
from yogya.config import Settings, get_settings
from yogya.data.loader import SchemeCorpus, load_corpus
from yogya.graph.lifecycle import build_lifecycle_graph, _coerce
from yogya.models.application import (
    Appeal,
    Application,
    ApplicationStatus,
    AuditEntry,
    DocumentGap,
    RejectionReason,
    RunMetrics,
)
from yogya.models.household import (
    DocumentKind,
    Gender,
    Household,
    Member,
    Occupation,
    RationCardType,
    Residence,
    SocialCategory,
)
from yogya.models.scheme import (
    BenefitKind,
    Criterion,
    CriterionResult,
    EligibilityResult,
    EligibilityVerdict,
    Operator,
    Scheme,
)
from yogya.models.state import OrchestratorState, YogyaState
from yogya.portal import PortalGateway, SimulatedPortal
from yogya.retrieval.index import SchemeRetriever
from yogya.store import CaseStore


class EligibilityTask(dict):
    """Payload carried by one Send to the map node."""


def build_graph(
    *,
    llm=None,
    corpus: SchemeCorpus | None = None,
    portal: PortalGateway | None = None,
    case_store: CaseStore | None = None,
    settings: Settings | None = None,
    checkpointer=None,
    as_of: date | None = None,
):
    """Compile the full Yogya graph.

    Every dependency is injectable, which is why the test suite never needs a
    network, a database, or an API key.
    """
    settings = settings or get_settings()
    corpus = corpus or load_corpus(settings.corpus_dir)
    portal = portal or SimulatedPortal()
    case_store = case_store or CaseStore()
    if llm is None:
        from yogya.llm import get_llm

        llm = get_llm(settings)

    retriever = SchemeRetriever(corpus, on=as_of)
    profiler = HouseholdProfiler(llm)
    matcher = EntitlementMatcher(llm)
    lifecycle = build_lifecycle_graph(
        llm=llm, corpus=corpus, portal=portal, settings=settings
    )

    # ---- nodes ------------------------------------------------------------
    def load_case(state: OrchestratorState) -> dict:
        """Pick up an existing case, if this family has been seen before."""
        household_id = state["household_id"]
        prior = case_store.load_profile(household_id)
        audit = entry(
            household_id=household_id,
            actor="case_store",
            action="opened case",
            detail=(
                f"existing case found, last profiled {prior.profiled_on}"
                if prior
                else "new case"
            ),
        )
        return {
            "audit": [audit],
            "corpus_snapshot": corpus.snapshot_id,
            "metrics": RunMetrics(),
            "applications": [],
        }

    def profile(state: OrchestratorState) -> dict:
        household_id = state["household_id"]
        household, audit = profiler.run(
            household_id=household_id,
            intake_notes=state.get("intake_notes", ""),
            known=case_store.known_fields(household_id),
        )
        return {"household": household, "audit": [audit]}

    def confirm_profile(state: OrchestratorState) -> dict:
        """Human gate before screening. Skippable only in tests."""
        household = state["household"]
        unresolved = profiler.needs_confirmation(household)

        if not settings.require_human_approval:
            return {"household": household}

        raw = interrupt(
            confirm_profile_request(household, unresolved).model_dump(mode="json")
        )
        decision = _coerce(raw, ProfileDecision)

        if decision.corrections:
            data = household.model_dump()
            data.update(decision.corrections)
            data["unknown_fields"] = [
                f for f in household.unknown_fields if f not in decision.corrections
            ]
            household = household.model_validate(data)

        audit = entry(
            household_id=household.household_id,
            actor="social_worker",
            action=f"profile {decision.action}ed",
            detail=(
                f"corrected {len(decision.corrections)} field(s): "
                f"{', '.join(decision.corrections) or 'none'}"
                + (f"; note: {decision.note}" if decision.note else "")
            ),
        )
        case_store.save_profile(household)
        return {
            "household": household,
            "audit": [audit],
            "metrics": RunMetrics(human_interrupts=1),
        }

    def retrieve(state: OrchestratorState) -> dict:
        household = state["household"]
        candidates = retriever.retrieve(household, top_k=settings.retrieval_top_k)
        scheme_ids = [c.scheme.scheme_id for c in candidates]
        audit = entry(
            household_id=household.household_id,
            actor="scheme_retriever",
            action="retrieved candidate schemes",
            detail=(
                f"{len(scheme_ids)} schemes applicable to a "
                f"{household.state} household; corpus snapshot {corpus.snapshot_id}"
            ),
        )
        return {
            "candidate_scheme_ids": scheme_ids,
            "audit": [audit],
            "metrics": RunMetrics(schemes_screened=len(scheme_ids)),
        }

    def fan_out(state: OrchestratorState) -> list[Send]:
        """The map step: one parallel branch per candidate scheme."""
        household = state["household"]
        return [
            Send(
                "check_eligibility",
                EligibilityTask(household=household, scheme_id=scheme_id),
            )
            for scheme_id in state["candidate_scheme_ids"]
        ]

    def check_eligibility(task: EligibilityTask) -> dict:
        """One branch of the map. Pure apart from the explanation call."""
        household = task["household"]
        scheme = corpus.get(task["scheme_id"])
        result, audit = matcher.run(household, scheme)
        return {"eligibility": [result], "audit": [audit]}

    def collect_results(state: OrchestratorState) -> dict:
        """The reduce step."""
        results = state.get("eligibility", [])
        eligible = [r for r in results if r.verdict is EligibilityVerdict.ELIGIBLE]
        needs_info = [r for r in results if r.verdict is EligibilityVerdict.NEEDS_INFO]

        audit = entry(
            household_id=state["household"].household_id,
            actor="entitlement_matcher",
            action="completed eligibility sweep",
            detail=(
                f"{len(eligible)} eligible, {len(needs_info)} need more information, "
                f"{len(results) - len(eligible) - len(needs_info)} not eligible"
            ),
        )
        return {
            "audit": [audit],
            "metrics": RunMetrics(benefits_discovered=len(eligible)),
        }

    def select_schemes(state: OrchestratorState) -> dict:
        """Human gate: which of the discovered benefits to actually file."""
        results = state.get("eligibility", [])
        eligible = [r for r in results if r.verdict is EligibilityVerdict.ELIGIBLE]
        needs_info = [r for r in results if r.verdict is EligibilityVerdict.NEEDS_INFO]
        household_id = state["household"].household_id

        if not settings.require_human_approval:
            return {"selected_scheme_ids": [r.scheme_id for r in eligible]}

        raw = interrupt(
            select_schemes_request(household_id, eligible, needs_info).model_dump(
                mode="json"
            )
        )
        selection = (
            raw
            if isinstance(raw, SchemeSelection)
            else SchemeSelection.model_validate(raw if isinstance(raw, dict) else {})
        )

        audit = entry(
            household_id=household_id,
            actor="social_worker",
            action="selected schemes to file",
            detail=(
                f"{len(selection.file_scheme_ids)} selected; "
                f"{len(selection.already_receiving_scheme_ids)} already received"
                + (f"; note: {selection.note}" if selection.note else "")
            ),
        )
        return {
            "selected_scheme_ids": selection.file_scheme_ids,
            "already_receiving_scheme_ids": selection.already_receiving_scheme_ids,
            "audit": [audit],
            "metrics": RunMetrics(
                human_interrupts=1,
                already_receiving=len(selection.already_receiving_scheme_ids),
            ),
        }

    def prepare_applications(state: OrchestratorState) -> dict:
        """Turn selections into draft Applications and queue them."""
        household = state["household"]
        by_scheme = {r.scheme_id: r for r in state.get("eligibility", [])}
        queue: list[Application] = []

        for scheme_id in state.get("selected_scheme_ids", []):
            scheme = corpus.get(scheme_id)
            result = by_scheme.get(scheme_id)
            queue.append(
                Application(
                    # Deterministic, not random: one application per household
                    # per scheme. Re-running a case therefore updates the same
                    # application rather than creating a duplicate, and the
                    # demo and tests are reproducible.
                    application_id=f"app-{household.household_id}-{scheme_id}",
                    household_id=household.household_id,
                    scheme_id=scheme_id,
                    scheme_name=scheme.name,
                    estimated_annual_value_inr=scheme.benefit_value_annual_inr,
                    rule_version=scheme.rule_version,
                )
            )

        audit = entry(
            household_id=household.household_id,
            actor="orchestrator",
            action="prepared applications",
            detail=f"{len(queue)} application(s) queued for filing",
        )
        return {
            "pending_applications": queue,
            "audit": [audit],
            "metrics": RunMetrics(applications_filed=len(queue)),
        }

    def next_application(state: OrchestratorState) -> dict:
        """Pop one application off the queue into the subgraph's input slot."""
        queue = list(state.get("pending_applications", []))
        if not queue:
            return {"current_application": None}
        current = queue.pop(0)
        return {
            "pending_applications": queue,
            "current_application": current,
            # `application` is the key the subgraph reads.
            "application": current,
            # Drain channels are cleared before each application so the
            # subgraph starts empty and `archive` folds in only its own work.
            "run_audit": [],
            "run_metrics": RunMetrics(),
        }

    def archive(state: OrchestratorState) -> dict:
        """Fold the finished application back into the accumulated list."""
        finished = state.get("application")
        if finished is None:
            return {}
        # Fold the subgraph's private accumulators into the run-wide ones,
        # exactly once.
        return {
            "applications": [finished],
            "audit": state.get("run_audit", []),
            "metrics": state.get("run_metrics", RunMetrics()),
        }

    def summarise(state: OrchestratorState) -> dict:
        household = state["household"]
        results = state.get("eligibility", [])
        applications = state.get("applications", [])
        metrics = state.get("metrics", RunMetrics())

        eligible = [r for r in results if r.verdict is EligibilityVerdict.ELIGIBLE]

        # Money recovered counts *cash the family receives*. An insurance
        # scheme's headline figure is a cover amount — the sum insured, payable
        # only on a claim — and adding it to a "rupees recovered" total would
        # inflate the number by an order of magnitude for no honest reason.
        # Cover is reported separately, under its own name.
        approved_apps = [
            a for a in applications if a.status is ApplicationStatus.APPROVED
        ]
        recovered = sum(
            a.estimated_annual_value_inr or 0
            for a in approved_apps
            if corpus.get(a.scheme_id).benefit_kind is not BenefitKind.INSURANCE
        )
        cover_secured = sum(
            a.estimated_annual_value_inr or 0
            for a in approved_apps
            if corpus.get(a.scheme_id).benefit_kind is BenefitKind.INSURANCE
        )
        wrongful_recovered = sum(
            1
            for a in applications
            if a.status is ApplicationStatus.APPROVED and a.appeal_attempts
        )

        summary = {
            "household_id": household.household_id,
            "head_name": household.head_name,
            "corpus_snapshot": corpus.snapshot_id,
            "schemes_screened": len(results),
            "benefits_discovered": len(eligible),
            "already_receiving": len(state.get("already_receiving_scheme_ids", [])),
            "applications_filed": len(applications),
            "approved": sum(
                1 for a in applications if a.status is ApplicationStatus.APPROVED
            ),
            "rejected": sum(
                1 for a in applications if a.status is ApplicationStatus.REJECTED
            ),
            "blocked_on_documents": sum(
                1
                for a in applications
                if a.status is ApplicationStatus.BLOCKED_ON_DOCUMENTS
            ),
            "escalated": sum(
                1 for a in applications if a.status is ApplicationStatus.ESCALATED
            ),
            "appeals_filed": sum(a.appeal_attempts for a in applications),
            "appeals_upheld": wrongful_recovered,
            "wrongful_denials_recovered": wrongful_recovered,
            "rupees_recovered_annual": recovered,
            "insurance_cover_secured_inr": cover_secured,
            "appeal_success_rate": round(metrics.appeal_success_rate, 3),
            "form_error_rate": round(metrics.form_error_rate, 3),
            "human_interrupts": metrics.human_interrupts,
        }

        audit = entry(
            household_id=household.household_id,
            actor="orchestrator",
            action="completed run",
            detail=(
                f"{summary['benefits_discovered']} benefits discovered, "
                f"{summary['applications_filed']} filed, "
                f"Rs {recovered:,} per year in cash recovered, "
                f"Rs {cover_secured:,} of insurance cover secured"
            ),
        )
        return {"summary": summary, "audit": [audit]}

    def persist(state: OrchestratorState) -> dict:
        household = state["household"]
        case_store.save_profile(household)
        case_store.save_applications(
            household.household_id, state.get("applications", [])
        )
        case_store.append_audit(household.household_id, state.get("audit", []))
        return {}

    # ---- edges ------------------------------------------------------------
    def has_more_applications(state: OrchestratorState) -> str:
        return "run" if state.get("current_application") is not None else "done"

    graph = StateGraph(OrchestratorState)
    graph.add_node("load_case", load_case)
    graph.add_node("profile", profile)
    graph.add_node("confirm_profile", confirm_profile)
    graph.add_node("retrieve", retrieve)
    graph.add_node("check_eligibility", check_eligibility)
    graph.add_node("collect_results", collect_results)
    graph.add_node("select_schemes", select_schemes)
    graph.add_node("prepare_applications", prepare_applications)
    graph.add_node("next_application", next_application)
    graph.add_node("application_lifecycle", lifecycle)
    graph.add_node("archive", archive)
    graph.add_node("summarise", summarise)
    graph.add_node("persist", persist)

    graph.add_edge(START, "load_case")
    graph.add_edge("load_case", "profile")
    graph.add_edge("profile", "confirm_profile")
    graph.add_edge("confirm_profile", "retrieve")
    graph.add_conditional_edges("retrieve", fan_out, ["check_eligibility"])
    graph.add_edge("check_eligibility", "collect_results")
    graph.add_edge("collect_results", "select_schemes")
    graph.add_edge("select_schemes", "prepare_applications")
    graph.add_edge("prepare_applications", "next_application")
    graph.add_conditional_edges(
        "next_application",
        has_more_applications,
        {"run": "application_lifecycle", "done": "summarise"},
    )
    graph.add_edge("application_lifecycle", "archive")
    graph.add_edge("archive", "next_application")
    graph.add_edge("summarise", "persist")
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=checkpointer or default_checkpointer())


# LangGraph's checkpoint serialiser refuses to deserialise arbitrary classes
# unless they are declared, and warns loudly about the ones it does not know.
# Declaring our own model modules keeps checkpoints strict-mode safe — an
# attacker who could write to the checkpoint store must not be able to smuggle
# in an arbitrary type — while staying quiet in normal operation.
YOGYA_CHECKPOINT_TYPES: tuple[type, ...] = (
    # household
    Household, Member, DocumentKind, Gender, Occupation, RationCardType,
    Residence, SocialCategory,
    # scheme
    Scheme, Criterion, CriterionResult, EligibilityResult, EligibilityVerdict,
    BenefitKind, Operator,
    # application
    Application, ApplicationStatus, Appeal, AuditEntry, DocumentGap,
    RejectionReason, RunMetrics,
)


def default_serializer() -> JsonPlusSerializer:
    """Checkpoint serialiser with our own model types declared.

    The allowlist is exhaustive on purpose: a type that is missing does not
    raise, it comes back as a plain dict, and the failure then surfaces
    somewhere far away as `'dict' object has no attribute ...`. If you add a
    model that travels through graph state, add it here.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=YOGYA_CHECKPOINT_TYPES)


def default_checkpointer() -> InMemorySaver:
    """In-memory checkpointing, which is the right default for a free tier.

    Swap in `langgraph.checkpoint.postgres.PostgresSaver` (same interface, same
    serialiser) when sessions need to survive a restart — see
    docs/DEPLOYMENT.md.
    """
    return InMemorySaver(serde=default_serializer())


# One application costs three super-steps (next_application, the subgraph,
# archive), so a family filing eight benefits needs well over LangGraph's
# default recursion limit of 25. Callers should pass this in the run config.
RECOMMENDED_RECURSION_LIMIT = 150


__all__ = ["build_graph", "RECOMMENDED_RECURSION_LIMIT"]
