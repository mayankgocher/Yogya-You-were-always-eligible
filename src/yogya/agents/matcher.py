"""Entitlement Matcher agent.

Evaluates one household against one scheme. Called once per scheme, in
parallel, by the map step of the outer graph.

The division of labour is the point:

    rule engine  ->  verdict   (deterministic, reproducible, citable)
    LLM          ->  wording   (never consulted about eligibility)

The LLM is handed the *finished* verdict and the criterion-by-criterion
results, and asked only to render them as a sentence a family can understand.
If the model is offline, unavailable, or wrong, the verdict is unchanged — the
explanation just falls back to a template. That property is what lets this run
on a free tier with no API key and still be correct.
"""

from __future__ import annotations

from pydantic import BaseModel

from yogya.audit import entry
from yogya.models.application import AuditEntry
from yogya.models.household import Household
from yogya.models.scheme import EligibilityResult, EligibilityVerdict, Scheme
from yogya.rules.engine import evaluate_scheme

AGENT_NAME = "entitlement_matcher"

SYSTEM_PROMPT = """\
You explain welfare eligibility decisions to families with low literacy.

You are given a decision that has ALREADY been made by a rule engine, plus the
individual criteria that produced it. Your job is only to phrase it.

Rules:
- Never contradict the verdict. Never re-decide.
- Never mention a criterion that is not in the list you were given.
- Two or three short sentences. Simple words. No jargon, no scheme numbers.
- If the verdict is not_eligible, say plainly which requirement was not met.
- If the verdict is needs_info, say exactly what the family must find out.
- If a failed requirement is marked remediable, say what would fix it.
"""


class ExplanationOutput(BaseModel):
    explanation: str
    plain_language_summary: str = ""


class EntitlementMatcher:
    """Agent 2 of 7."""

    name = AGENT_NAME

    def __init__(self, llm) -> None:
        self.llm = llm

    def run(
        self, household: Household, scheme: Scheme, explain: bool = True
    ) -> tuple[EligibilityResult, AuditEntry]:
        # 1. Decide. Deterministically. No model in this path.
        result = evaluate_scheme(household, scheme)

        # 2. Describe. Best-effort; failure here never changes the verdict.
        if explain:
            result.explanation = self._explain(result, scheme)
        else:
            result.explanation = _template_explanation(result)

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action=f"evaluated eligibility: {result.verdict.value}",
            detail=(
                f"{scheme.name}: "
                f"{sum(1 for r in result.results if r.passed)}/{len(result.results)} "
                f"criteria passed"
                + (
                    f"; failed on {', '.join(r.criterion_id for r in result.failed)}"
                    if result.failed
                    else ""
                )
            ),
            scheme_id=scheme.scheme_id,
            citations=result.citations,
            rule_version=scheme.rule_version,
        )
        return result, audit

    def _explain(self, result: EligibilityResult, scheme: Scheme) -> str:
        criteria_lines = "\n".join(
            f"- [{'PASS' if r.passed else 'UNKNOWN' if r.unknown else 'FAIL'}] "
            f"{r.description}"
            + (" (this can be fixed)" if r.remediable and not r.passed else "")
            for r in result.results
        )
        user = (
            f"Scheme: {scheme.name}\n"
            f"What it gives: {scheme.benefit_summary}\n"
            f"Verdict: {result.verdict.value}\n"
            f"Criteria:\n{criteria_lines}"
        )
        try:
            output = self.llm.structured(
                system=SYSTEM_PROMPT, user=user, schema=ExplanationOutput
            )
            return output.explanation or _template_explanation(result)
        except Exception:  # noqa: BLE001 — wording is never load-bearing
            return _template_explanation(result)


def _template_explanation(result: EligibilityResult) -> str:
    """Deterministic fallback wording, derived from the criteria alone."""
    if result.verdict is EligibilityVerdict.ELIGIBLE:
        return (
            f"This family meets all {len(result.results)} requirements for "
            f"{result.scheme_name}."
        )
    if result.verdict is EligibilityVerdict.NEEDS_INFO:
        unknown = [r.description for r in result.results if r.unknown]
        return (
            f"{result.scheme_name} cannot be decided yet. Still to confirm: "
            + "; ".join(unknown)
        )
    failed = result.failed
    fixable = [r.description for r in failed if r.remediable]
    hard = [r.description for r in failed if not r.remediable]
    parts = [f"This family does not qualify for {result.scheme_name}."]
    if hard:
        parts.append("Requirement not met: " + "; ".join(hard) + ".")
    if fixable:
        parts.append("This could be fixed: " + "; ".join(fixable) + ".")
    return " ".join(parts)
