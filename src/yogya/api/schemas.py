"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StartSessionRequest(BaseModel):
    household_id: str = Field(..., min_length=1, max_length=120)
    intake_notes: str = Field(..., min_length=1, max_length=20_000)


class ResumeRequest(BaseModel):
    """One human decision, answering whatever gate the session is waiting on.

    The shape is intentionally loose — the graph validates it against the
    specific decision model for that interrupt kind — so the frontend can post
    a plain object without knowing the union of decision types.
    """

    decision: dict[str, Any] = Field(default_factory=dict)


class SessionState(BaseModel):
    """What the console needs to render the session, in one payload."""

    thread_id: str
    household_id: str
    status: str  # "awaiting_human" | "complete"
    pending_interrupt: dict[str, Any] | None = None
    household: dict[str, Any] | None = None
    eligibility: list[dict[str, Any]] = Field(default_factory=list)
    applications: list[dict[str, Any]] = Field(default_factory=list)
    audit: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] | None = None
    corpus_snapshot: str | None = None


class SchemeSummary(BaseModel):
    scheme_id: str
    name: str
    level: str
    state: str | None = None
    benefit_kind: str
    benefit_summary: str
    benefit_value_annual_inr: int | None = None
    rule_version: str
    criteria_count: int
    source_url: str | None = None


class HealthResponse(BaseModel):
    status: str
    corpus_snapshot: str
    schemes: int
    llm_model: str
    llm_offline: bool
