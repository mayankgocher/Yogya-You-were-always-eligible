"""Form Filler agent.

Fills an application form from the household profile.

The metric this agent is judged on is form error rate, so its design goal is
not "fill every field" but "never fill a field wrongly". Values are copied from
structured profile data by a deterministic mapping. Anything the mapping cannot
supply is *flagged* rather than invented, and a flagged field blocks submission
until a human fills it.

The LLM's only job here is the genuinely linguistic one: transliterating names
into Devanagari when a form demands it, and composing the one or two free-text
declarations forms ask for. Neither can silently corrupt a factual field.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from yogya.audit import entry
from yogya.models.application import AuditEntry
from yogya.models.household import Household
from yogya.models.scheme import Scheme

AGENT_NAME = "form_filler"

FLAG = "<<NEEDS HUMAN>>"

SYSTEM_PROMPT = """\
You draft the free-text declaration on an Indian welfare application form.

You are given the household's structured facts. Write the declaration in the
first person, in simple language, stating only facts you were given.

Never state a fact you were not given. Never state an amount, a date, or a
document number that is not in the input. Two to four sentences.
"""


class DeclarationOutput(BaseModel):
    declaration: str
    fields_you_could_not_fill: list[str] = Field(default_factory=list)


# Form field name -> how to derive it from the household. Returning None means
# "cannot determine", which becomes a flagged field.
FIELD_MAP: dict[str, str] = {
    "applicant_name": "head_name",
    "state": "state",
    "district": "district",
    "residence_type": "residence",
    "annual_income": "annual_income",
    "social_category": "social_category",
    "ration_card_type": "ration_card",
    "household_size": "household_size",
    "land_holding_hectares": "land_holding_hectares",
    "bank_account_available": "has_bank_account",
}


class FormFillerAgent:
    """Agent 4 of 7."""

    name = AGENT_NAME

    def __init__(self, llm) -> None:
        self.llm = llm

    def run(
        self,
        household: Household,
        scheme: Scheme,
        application_id: str | None = None,
        include_declaration: bool = True,
    ) -> tuple[dict[str, str], list[str], AuditEntry]:
        """Return (form_data, flagged_field_names, audit_entry)."""
        context = household.rule_context()
        form: dict[str, str] = {}
        flagged: list[str] = []

        for field_name, source in FIELD_MAP.items():
            value = context.get(source, getattr(household, source, None))
            if value is None or value == "unknown":
                form[field_name] = FLAG
                flagged.append(field_name)
            else:
                form[field_name] = _render(value)

        # Scheme-specific fields that every application asks for.
        form["scheme_id"] = scheme.scheme_id
        form["scheme_name"] = scheme.name
        form["documents_enclosed"] = ", ".join(
            sorted(d.value for d in household.available_documents)
        ) or FLAG
        if form["documents_enclosed"] == FLAG:
            flagged.append("documents_enclosed")

        if include_declaration:
            form["declaration"] = self._declaration(household, scheme)
        else:
            form["declaration"] = _template_declaration(household, scheme)

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action="filled application form",
            detail=(
                f"{len(form)} fields filled, {len(flagged)} flagged for human "
                f"entry: {', '.join(flagged) or 'none'}"
            ),
            scheme_id=scheme.scheme_id,
            application_id=application_id,
        )
        return form, flagged, audit

    def _declaration(self, household: Household, scheme: Scheme) -> str:
        facts = "\n".join(
            f"- {k}: {v}"
            for k, v in household.rule_context().items()
            if v is not None and v != "unknown"
        )
        try:
            output = self.llm.structured(
                system=SYSTEM_PROMPT,
                user=(
                    f"Scheme applied for: {scheme.name}\n"
                    f"Applicant: {household.head_name}\n"
                    f"Known facts:\n{facts}"
                ),
                schema=DeclarationOutput,
            )
            return output.declaration or _template_declaration(household, scheme)
        except Exception:  # noqa: BLE001
            return _template_declaration(household, scheme)

    @staticmethod
    def is_submittable(form: dict[str, str]) -> bool:
        """A form with any flagged field must not reach a government portal."""
        return not any(v == FLAG for v in form.values())

    @staticmethod
    def flagged_fields(form: dict[str, str]) -> list[str]:
        return sorted(k for k, v in form.items() if v == FLAG)


def _render(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (set, frozenset)):
        return ", ".join(sorted(str(v) for v in value))
    return str(value)


def _template_declaration(household: Household, scheme: Scheme) -> str:
    return (
        f"I, {household.head_name}, resident of "
        f"{household.district or household.state}, apply for {scheme.name}. "
        "I declare that the particulars given in this form are true to the best "
        "of my knowledge and belief."
    )
