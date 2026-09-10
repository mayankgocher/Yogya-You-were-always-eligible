"""Scheme and eligibility-rule models.

Design note that matters: a scheme's eligibility is expressed as a list of
*structured, machine-checkable* criteria — never as prose for an LLM to judge.
The LLM explains and drafts; the rule engine decides. This is what makes the
system auditable and what makes "wrongly denied" a claim we can defend.

Rules are versioned because scheme rules genuinely change (income ceilings are
revised, states add categories). A decision always records which rule version
produced it.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from yogya.models.household import DocumentKind


class Operator(str, Enum):
    LTE = "lte"
    LT = "lt"
    GTE = "gte"
    GT = "gt"
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    INTERSECTS = "intersects"


class BenefitKind(str, Enum):
    CASH_TRANSFER = "cash_transfer"
    PENSION = "pension"
    SUBSIDY = "subsidy"
    INSURANCE = "insurance"
    SCHOLARSHIP = "scholarship"
    HOUSING = "housing"
    FOOD = "food"
    SERVICE = "service"


class Criterion(BaseModel):
    """One machine-checkable eligibility condition.

    `field` names a key produced by Household.rule_context(). `citation` points
    at the clause of the scheme document that this criterion encodes — it is
    surfaced to the social worker and written into the audit log.
    """

    criterion_id: str
    field: str
    op: Operator
    value: Any = None
    description: str
    citation: str
    # A criterion the family can plausibly fix (get a certificate, open an
    # account) rather than an immutable bar.
    remediable: bool = False
    # When the field is unknown, does the criterion block or merely warn?
    unknown_is_blocking: bool = False


class Scheme(BaseModel):
    """A single benefit scheme with structured eligibility."""

    scheme_id: str
    name: str
    name_local: str | None = None
    level: Literal["central", "state", "local"] = "central"
    state: str | None = None  # None for central schemes
    ministry: str | None = None
    benefit_kind: BenefitKind = BenefitKind.SERVICE
    benefit_summary: str
    # Approximate annual rupee value; used only for the "rupees recovered"
    # metric and always labelled as an estimate in the UI.
    benefit_value_annual_inr: int | None = None

    description: str = ""
    criteria: list[Criterion] = Field(default_factory=list)
    required_documents: list[DocumentKind] = Field(default_factory=list)
    optional_documents: list[DocumentKind] = Field(default_factory=list)
    application_mode: Literal["online", "offline", "both"] = "both"
    application_url: str | None = None
    grievance_url: str | None = None
    appeal_window_days: int = 30

    # --- versioning -------------------------------------------------------
    rule_version: str = "1.0.0"
    effective_from: date | None = None
    superseded_on: date | None = None
    source_url: str | None = None
    source_snapshot: str | None = None  # corpus snapshot id this came from

    keywords: list[str] = Field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.superseded_on is None

    def searchable_text(self) -> str:
        parts = [
            self.name,
            self.name_local or "",
            self.benefit_summary,
            self.description,
            self.benefit_kind.value,
            self.state or "central",
            " ".join(self.keywords),
            " ".join(c.description for c in self.criteria),
        ]
        return " ".join(p for p in parts if p)


class CriterionResult(BaseModel):
    """Outcome of evaluating one criterion — the unit of explainability."""

    criterion_id: str
    passed: bool
    unknown: bool = False
    actual: Any = None
    expected: Any = None
    description: str
    citation: str
    remediable: bool = False


class EligibilityVerdict(str, Enum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_INFO = "needs_info"


class EligibilityResult(BaseModel):
    """Per-scheme decision for one household, fully traceable."""

    scheme_id: str
    scheme_name: str
    household_id: str
    verdict: EligibilityVerdict
    rule_version: str
    results: list[CriterionResult] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    # Plain-language explanation produced by the LLM *from* the results above.
    explanation: str = ""
    estimated_annual_value_inr: int | None = None

    @property
    def failed(self) -> list[CriterionResult]:
        return [r for r in self.results if not r.passed and not r.unknown]

    @property
    def blocking_failures(self) -> list[CriterionResult]:
        return [r for r in self.failed if not r.remediable]

    @property
    def citations(self) -> list[str]:
        return sorted({r.citation for r in self.results if r.citation})
