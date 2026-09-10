"""Household Profiler agent.

Turns a social worker's free-text intake note into a structured Household.

This is the one place where an LLM is genuinely the right tool: the input is
messy human narrative in mixed Hindi and English ("bade beta ka naam Ramesh,
umar 19, college mein padhta hai"), and the output is a fixed schema. It is
also the highest-risk place, because an invented income figure would silently
corrupt every downstream verdict.

Two guards, therefore:

* The model may answer "unknown" for anything, and is instructed to prefer it
  over a guess. Unknowns propagate into NEEDS_INFO verdicts rather than being
  quietly defaulted to zero.
* Every field the model was unsure about is listed in `unknown_fields`, which
  the graph turns into a human confirmation step before anything is filed.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from yogya.audit import entry
from yogya.models.application import AuditEntry
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

AGENT_NAME = "household_profiler"

SYSTEM_PROMPT = """\
You are an intake assistant for a welfare-benefits caseworker in India.

Convert the caseworker's notes into structured household data.

Absolute rules:
- NEVER invent a value. If the notes do not state or clearly imply something,
  use the "unknown" option (or null for numbers) and list the field name in
  unknown_fields.
- Annual income means the whole household's income in rupees per year. If the
  notes give a monthly figure, multiply by 12 and say so in reasoning.
- Notes may mix Hindi and English. Interpret both.
- Ration card: "yellow"/"Antyodaya"/"AAY" -> aay; "white"/"priority"/"PHH" ->
  phh; "APL" -> apl; explicitly none -> none; unclear -> unknown.
- A person described as a widow sets is_widow on that member, not on others.
- Land in bigha/katha is not hectares. If the unit is unclear, set land to null
  and list land_holding_hectares in unknown_fields.

Prefer "unknown" over a plausible guess. A missing field costs one follow-up
question. A wrong field costs a family their benefit.
"""


class MemberDraft(BaseModel):
    name: str
    relation: str = "member"
    age: int | None = None
    gender: Gender = Gender.UNKNOWN
    is_student: bool = False
    school_level: str | None = None
    has_disability: bool = False
    disability_percent: int | None = None
    is_pregnant: bool = False
    is_widow: bool = False
    occupation: Occupation = Occupation.UNKNOWN


class ProfileOutput(BaseModel):
    """Schema the model must fill. Mirrors Household minus derived fields."""

    head_name: str
    state: str
    district: str | None = None
    residence: Residence = Residence.UNKNOWN
    annual_income: int | None = None
    social_category: SocialCategory = SocialCategory.UNKNOWN
    ration_card: RationCardType = RationCardType.UNKNOWN
    land_holding_hectares: float | None = None
    owns_pucca_house: bool | None = None
    has_bank_account: bool | None = None
    has_electricity: bool | None = None
    has_lpg_connection: bool | None = None
    is_income_tax_payer: bool | None = None
    members: list[MemberDraft] = Field(default_factory=list)
    documents: list[DocumentKind] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    reasoning: str = ""


# Fields whose absence materially changes which schemes a family sees. If the
# profiler leaves one of these unknown, the graph asks a human rather than
# proceeding on a guess.
CRITICAL_FIELDS = {
    "state",
    "residence",
    "annual_income",
    "ration_card",
    "social_category",
}


class HouseholdProfiler:
    """Agent 1 of 7."""

    name = AGENT_NAME

    def __init__(self, llm) -> None:
        self.llm = llm

    def run(
        self,
        *,
        household_id: str,
        intake_notes: str,
        known: dict | None = None,
    ) -> tuple[Household, AuditEntry]:
        """Extract a Household from notes, merged over any known fields.

        `known` lets a returning family skip re-intake: last month's confirmed
        profile is passed in, and the notes only need to carry what changed.
        """
        output = self.llm.structured(
            system=SYSTEM_PROMPT,
            user=f"Caseworker notes:\n---\n{intake_notes}\n---",
            schema=ProfileOutput,
        )

        data = output.model_dump(exclude={"reasoning", "members", "unknown_fields"})
        if known:
            # Confirmed prior values win over a fresh extraction unless the new
            # extraction is more specific (prior unknown, new known).
            for key, prior in known.items():
                if key in data and _is_unknown(data[key]) and not _is_unknown(prior):
                    data[key] = prior

        members = [
            Member(member_id=f"m{i + 1}", **m.model_dump())
            for i, m in enumerate(output.members)
        ]

        unknown = sorted(set(output.unknown_fields) | _detect_unknowns(data))

        household = Household(
            household_id=household_id,
            members=members,
            intake_notes=intake_notes,
            profiled_on=date.today(),
            unknown_fields=unknown,
            **data,
        )

        audit = entry(
            household_id=household_id,
            actor=self.name,
            action="profiled household",
            detail=(
                f"{len(members)} members; "
                f"{len(unknown)} field(s) left unknown: {', '.join(unknown) or 'none'}"
            ),
        )
        return household, audit

    @staticmethod
    def needs_confirmation(household: Household) -> list[str]:
        """Critical fields the human must resolve before anything is filed."""
        return sorted(set(household.unknown_fields) & CRITICAL_FIELDS)


def _is_unknown(value) -> bool:
    return value is None or (isinstance(value, str) and value == "unknown")


def _detect_unknowns(data: dict) -> set[str]:
    """Catch unknowns the model forgot to declare."""
    return {k for k, v in data.items() if _is_unknown(v)}
