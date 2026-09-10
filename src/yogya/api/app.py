"""FastAPI service.

One process serves both the API and the static console, so the whole thing
deploys as a single free-tier web service — no separate frontend host, no CORS
dance, no second thing to keep awake.

Sessions are graph threads. Starting a session runs until the first human gate;
every subsequent POST answers the gate the session is waiting on and runs until
the next. That maps exactly onto how a caseworker actually works, and it means
the API has only two verbs to get right.

State lives in the process (`InMemorySaver` + `InMemoryStore`), which is the
correct trade for a free tier that sleeps when idle. `build_app` takes a
checkpointer and store, so pointing this at Postgres is a wiring change, not a
rewrite — see docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from yogya.api.schemas import (
    HealthResponse,
    ResumeRequest,
    SchemeSummary,
    SessionState,
    StartSessionRequest,
)
from yogya.config import Settings, get_settings
from yogya.data.loader import load_corpus
from yogya.graph.build import build_graph
from yogya.graph.runner import pending_interrupt, run_config
from yogya.portal import SimulatedPortal
from yogya.retrieval.index import SchemeRetriever
from yogya.store import CaseStore

logger = logging.getLogger(__name__)


def build_app(
    *,
    settings: Settings | None = None,
    llm=None,
    corpus=None,
    portal=None,
    case_store: CaseStore | None = None,
    checkpointer=None,
) -> FastAPI:
    settings = settings or get_settings()
    corpus = corpus or load_corpus(settings.corpus_dir)
    portal = portal or SimulatedPortal()
    case_store = case_store or CaseStore()
    retriever = SchemeRetriever(corpus)

    graph = build_graph(
        llm=llm,
        corpus=corpus,
        portal=portal,
        case_store=case_store,
        settings=settings,
        checkpointer=checkpointer,
    )

    app = FastAPI(title=settings.api_title, version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Handy for tests and for anything that wants the same instances.
    app.state.graph = graph
    app.state.corpus = corpus
    app.state.portal = portal
    app.state.case_store = case_store
    app.state.settings = settings

    # ---- helpers ----------------------------------------------------------
    def snapshot(thread_id: str) -> SessionState:
        config = run_config(thread_id)
        values = graph.get_state(config).values
        if not values:
            raise HTTPException(status_code=404, detail=f"unknown session {thread_id}")
        interrupt_payload = pending_interrupt(graph, config)

        household = values.get("household")
        return SessionState(
            thread_id=thread_id,
            household_id=values.get("household_id", ""),
            status="awaiting_human" if interrupt_payload else "complete",
            pending_interrupt=interrupt_payload,
            household=household.model_dump(mode="json") if household else None,
            eligibility=[
                r.model_dump(mode="json") for r in values.get("eligibility", [])
            ],
            applications=[
                a.model_dump(mode="json") for a in values.get("applications", [])
            ],
            audit=[e.model_dump(mode="json") for e in values.get("audit", [])],
            summary=values.get("summary"),
            corpus_snapshot=values.get("corpus_snapshot"),
        )

    # ---- routes -----------------------------------------------------------
    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            corpus_snapshot=corpus.snapshot_id,
            schemes=len(corpus),
            llm_model=settings.llm_model,
            llm_offline=settings.llm_offline,
        )

    @app.get("/api/schemes", response_model=list[SchemeSummary])
    def list_schemes(state: str | None = None, q: str | None = None):
        """Browse the corpus. The 'show me the rules' view."""
        if q:
            schemes = [c.scheme for c in retriever.search(q, top_k=50)]
        elif state:
            schemes = corpus.for_household(state)
        else:
            schemes = corpus.current()
        return [
            SchemeSummary(
                scheme_id=s.scheme_id,
                name=s.name,
                level=s.level,
                state=s.state,
                benefit_kind=s.benefit_kind.value,
                benefit_summary=s.benefit_summary,
                benefit_value_annual_inr=s.benefit_value_annual_inr,
                rule_version=s.rule_version,
                criteria_count=len(s.criteria),
                source_url=s.source_url,
            )
            for s in schemes
        ]

    @app.get("/api/schemes/{scheme_id}")
    def scheme_detail(scheme_id: str, version: str | None = None) -> dict[str, Any]:
        try:
            scheme = corpus.get(scheme_id, version=version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **scheme.model_dump(mode="json"),
            "available_versions": corpus.versions_of(scheme_id),
        }

    @app.post("/api/sessions", response_model=SessionState)
    def start_session(request: StartSessionRequest) -> SessionState:
        """Begin a screening. Runs until the first human gate."""
        thread_id = f"{request.household_id}:{date.today():%Y%m%d%H%M%S}"
        config = run_config(thread_id)
        graph.invoke(
            {
                "household_id": request.household_id,
                "intake_notes": request.intake_notes,
            },
            config,
        )
        return snapshot(thread_id)

    @app.get("/api/sessions/{thread_id}", response_model=SessionState)
    def get_session(thread_id: str) -> SessionState:
        return snapshot(thread_id)

    @app.post("/api/sessions/{thread_id}/resume", response_model=SessionState)
    def resume_session(thread_id: str, request: ResumeRequest) -> SessionState:
        """Answer the gate the session is waiting on."""
        config = run_config(thread_id)
        if pending_interrupt(graph, config) is None:
            raise HTTPException(
                status_code=409,
                detail="this session is not waiting for a decision",
            )
        if not request.decision:
            # An empty payload must never be treated as "accept the defaults".
            # SubmissionDecision defaults to action="submit", so a stray empty
            # POST would file an application with a government department.
            # LangGraph also declines to resume on a falsy value, so this would
            # otherwise present as the gate mysteriously reappearing.
            raise HTTPException(
                status_code=422,
                detail=(
                    "decision must not be empty; send the action you are "
                    "taking, e.g. {\"action\": \"confirm\"}"
                ),
            )
        try:
            graph.invoke(Command(resume=request.decision), config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("resume failed for %s", thread_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return snapshot(thread_id)

    @app.get("/api/households/{household_id}/case")
    def get_case(household_id: str) -> dict[str, Any]:
        """The long-lived case, independent of any single run."""
        case = case_store.load_case(household_id)
        if case["household"] is None and not case["applications"]:
            raise HTTPException(status_code=404, detail="no case for this household")
        return {
            "household": case["household"].model_dump(mode="json")
            if case["household"]
            else None,
            "applications": [a.model_dump(mode="json") for a in case["applications"]],
            "audit": [e.model_dump(mode="json") for e in case["audit"]],
        }

    # ---- static console ---------------------------------------------------
    web_dir = settings.web_dir
    if web_dir.is_dir():
        app.mount(
            "/static", StaticFiles(directory=str(web_dir)), name="static"
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(web_dir / "index.html"))

    return app


# Module-level app for `uvicorn yogya.api.app:app`
app = build_app()
