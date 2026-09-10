"""Application lifecycle models — the long-lived, rejection-aware part.

An application is not a request/response. It is a small state machine that can
sit in WAITING for weeks, come back REJECTED, spawn an appeal, and be revived.
Everything about it must survive process restarts, which is why it is a plain
serialisable model persisted in the LangGraph Store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from yogya.models.household import DocumentKind


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApplicationStatus(str, Enum):
    DRAFT = "draft"
    BLOCKED_ON_DOCUMENTS = "blocked_on_documents"
    AWAITING_APPROVAL = "awaiting_approval"  # human must approve before submit
    SUBMITTED = "submitted"
    WAITING = "waiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPEALED = "appealed"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


TERMINAL_STATUSES = {
    ApplicationStatus.APPROVED,
    ApplicationStatus.ESCALATED,
    ApplicationStatus.ABANDONED,
}


class DocumentGap(BaseModel):
    """A missing document plus, crucially, how to actually get it."""

    document: DocumentKind
    required_for: str
    holder_member_id: str | None = None
    where_to_get: str
    typical_days: int | None = None
    cost_inr: int | None = None
    blocking: bool = True


class RejectionReason(BaseModel):
    code: str
    text: str
    # Set by the grievance agent: is this rejection defensible or wrong?
    assessed_as_wrongful: bool | None = None
    assessment_note: str = ""


class Appeal(BaseModel):
    appeal_id: str
    attempt: int
    grounds: str
    letter_text: str
    citations: list[str] = Field(default_factory=list)
    filed_at: datetime | None = None
    outcome: str | None = None  # "upheld" | "dismissed" | None


class Application(BaseModel):
    application_id: str
    household_id: str
    scheme_id: str
    scheme_name: str
    status: ApplicationStatus = ApplicationStatus.DRAFT

    form_data: dict[str, str] = Field(default_factory=dict)
    document_gaps: list[DocumentGap] = Field(default_factory=list)
    attached_documents: list[DocumentKind] = Field(default_factory=list)

    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    reference_number: str | None = None

    rejection: RejectionReason | None = None
    appeals: list[Appeal] = Field(default_factory=list)
    appeal_attempts: int = 0

    estimated_annual_value_inr: int | None = None
    rule_version: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def blocking_gaps(self) -> list[DocumentGap]:
        return [g for g in self.document_gaps if g.blocking]

    def touch(self) -> None:
        self.updated_at = _now()


class AuditEntry(BaseModel):
    """One immutable line in the household's audit trail.

    Every claim the system makes about a family must be traceable to one of
    these. `citations` is what separates a defensible claim from a guess.
    """

    entry_id: str
    household_id: str
    actor: str  # agent name, or "social_worker"
    action: str
    detail: str = ""
    scheme_id: str | None = None
    application_id: str | None = None
    citations: list[str] = Field(default_factory=list)
    rule_version: str | None = None
    at: datetime = Field(default_factory=_now)


class RunMetrics(BaseModel):
    """The numbers the pitch promises, computed rather than asserted."""

    schemes_screened: int = 0
    benefits_discovered: int = 0
    already_receiving: int = 0
    applications_filed: int = 0
    applications_approved: int = 0
    applications_rejected: int = 0
    appeals_filed: int = 0
    appeals_upheld: int = 0
    grievances_escalated: int = 0
    rupees_recovered_annual: int = 0
    form_fields_total: int = 0
    form_fields_flagged: int = 0
    human_interrupts: int = 0

    @property
    def appeal_success_rate(self) -> float:
        return self.appeals_upheld / self.appeals_filed if self.appeals_filed else 0.0

    @property
    def form_error_rate(self) -> float:
        if not self.form_fields_total:
            return 0.0
        return self.form_fields_flagged / self.form_fields_total
