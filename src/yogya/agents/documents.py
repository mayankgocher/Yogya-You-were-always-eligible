"""Document Gap agent.

Names what is missing and — the part that actually helps — where to get it.

This agent is deliberately almost LLM-free. "Where do I get an income
certificate in Uttar Pradesh" has one correct answer, and a hallucinated office
name sends a family on a wasted day's travel they paid bus fare for. So the
guidance comes from a curated table, and the model is used only to sequence the
errands sensibly when there are several.
"""

from __future__ import annotations

from yogya.audit import entry
from yogya.models.application import AuditEntry, DocumentGap
from yogya.models.household import DocumentKind, Household
from yogya.models.scheme import Scheme

AGENT_NAME = "document_gap"


# Curated, citable guidance. `typical_days` and `cost_inr` are indicative and
# are shown to the caseworker as estimates, never as promises.
DOCUMENT_GUIDANCE: dict[DocumentKind, dict] = {
    DocumentKind.AADHAAR: {
        "where": "Nearest Aadhaar Seva Kendra or a bank/post office enrolment centre. Free for first enrolment and for the mandatory update.",
        "days": 30,
        "cost": 0,
    },
    DocumentKind.RATION_CARD: {
        "where": "Block Supply Office (rural) or Tehsil/Circle Office (urban), or the state food department portal. Requires Aadhaar of all members and a residence proof.",
        "days": 30,
        "cost": 0,
    },
    DocumentKind.INCOME_CERTIFICATE: {
        "where": "Apply at the Tehsildar's office or a CSC/Jan Seva Kendra. Usually issued within a month after a lekhpal's field verification.",
        "days": 21,
        "cost": 30,
    },
    DocumentKind.CASTE_CERTIFICATE: {
        "where": "Tehsildar's office or CSC, with a parent's caste certificate or a school leaving certificate showing category.",
        "days": 21,
        "cost": 30,
    },
    DocumentKind.DOMICILE_CERTIFICATE: {
        "where": "Tehsildar's office or CSC, with proof of residence for the qualifying period.",
        "days": 21,
        "cost": 30,
    },
    DocumentKind.BANK_PASSBOOK: {
        "where": "Any bank branch or post office. A zero-balance Jan Dhan account can be opened with Aadhaar alone and cannot be refused.",
        "days": 3,
        "cost": 0,
    },
    DocumentKind.LAND_RECORD: {
        "where": "Khatauni/khasra extract from the lekhpal or the state land records portal.",
        "days": 7,
        "cost": 20,
    },
    DocumentKind.DISABILITY_CERTIFICATE: {
        "where": "District hospital medical board, or apply through the UDID portal. Assessment happens on scheduled board days.",
        "days": 45,
        "cost": 0,
    },
    DocumentKind.BIRTH_CERTIFICATE: {
        "where": "Gram Panchayat secretary or municipal registrar of births. Late registration needs an affidavit.",
        "days": 15,
        "cost": 20,
    },
    DocumentKind.DEATH_CERTIFICATE: {
        "where": "Gram Panchayat secretary or municipal registrar of deaths, where the death was registered.",
        "days": 15,
        "cost": 20,
    },
    DocumentKind.MARRIAGE_CERTIFICATE: {
        "where": "Registrar of Marriages at the tehsil, or via the state marriage registration portal.",
        "days": 30,
        "cost": 100,
    },
    DocumentKind.SCHOOL_CERTIFICATE: {
        "where": "The student's school office — a bonafide certificate is issued on request and is normally free.",
        "days": 3,
        "cost": 0,
    },
    DocumentKind.LABOUR_CARD: {
        "where": "Register with the state Building and Other Construction Workers Welfare Board at the labour office or its portal; needs 90 days' work certificate from the employer or a self-declaration.",
        "days": 30,
        "cost": 20,
    },
    DocumentKind.ELECTRICITY_BILL: {
        "where": "The local electricity distribution office, or download from the discom portal using the consumer number.",
        "days": 1,
        "cost": 0,
    },
    DocumentKind.PHOTOGRAPH: {
        "where": "Any photo studio or CSC.",
        "days": 1,
        "cost": 50,
    },
}


class DocumentGapAgent:
    """Agent 3 of 7."""

    name = AGENT_NAME

    def __init__(self, llm=None) -> None:
        self.llm = llm  # accepted for interface symmetry; guidance is curated

    def run(
        self, household: Household, scheme: Scheme, application_id: str | None = None
    ) -> tuple[list[DocumentGap], AuditEntry]:
        available = household.available_documents
        gaps: list[DocumentGap] = []

        for doc in scheme.required_documents:
            if doc in available:
                continue
            guidance = DOCUMENT_GUIDANCE.get(doc, {})
            gaps.append(
                DocumentGap(
                    document=doc,
                    required_for=scheme.name,
                    holder_member_id=_likely_holder(household, doc),
                    where_to_get=guidance.get(
                        "where", "Ask at the block office for the issuing authority."
                    ),
                    typical_days=guidance.get("days"),
                    cost_inr=guidance.get("cost"),
                    blocking=True,
                )
            )

        for doc in scheme.optional_documents:
            if doc in available:
                continue
            guidance = DOCUMENT_GUIDANCE.get(doc, {})
            gaps.append(
                DocumentGap(
                    document=doc,
                    required_for=scheme.name,
                    where_to_get=guidance.get("where", "Optional supporting document."),
                    typical_days=guidance.get("days"),
                    cost_inr=guidance.get("cost"),
                    blocking=False,
                )
            )

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action="checked document gaps",
            detail=(
                f"{scheme.name}: {len(gaps)} missing "
                f"({sum(1 for g in gaps if g.blocking)} blocking)"
                if gaps
                else f"{scheme.name}: all required documents present"
            ),
            scheme_id=scheme.scheme_id,
            application_id=application_id,
        )
        return gaps, audit

    @staticmethod
    def errand_plan(gaps: list[DocumentGap]) -> list[DocumentGap]:
        """Order the errands so the family makes the fewest trips.

        Blocking first, then by turnaround time descending — the slowest
        certificate should be applied for on day one, not last.
        """
        return sorted(
            gaps,
            key=lambda g: (not g.blocking, -(g.typical_days or 0), g.document.value),
        )

    @staticmethod
    def total_cost(gaps: list[DocumentGap]) -> int:
        return sum(g.cost_inr or 0 for g in gaps)

    @staticmethod
    def longest_wait(gaps: list[DocumentGap]) -> int:
        return max((g.typical_days or 0 for g in gaps), default=0)


def _likely_holder(household: Household, doc: DocumentKind) -> str | None:
    """Whose document is it? Only obvious cases; None means 'household level'."""
    if doc is DocumentKind.DISABILITY_CERTIFICATE:
        member = next((m for m in household.members if m.has_disability), None)
        return member.member_id if member else None
    if doc is DocumentKind.SCHOOL_CERTIFICATE:
        member = next((m for m in household.members if m.is_student), None)
        return member.member_id if member else None
    if doc is DocumentKind.DEATH_CERTIFICATE:
        member = next((m for m in household.members if m.is_widow), None)
        return member.member_id if member else None
    return None
