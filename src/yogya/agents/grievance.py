"""Grievance Escalation agent.

Decides whether a rejection was wrong, drafts the appeal, and — when appeals
are exhausted — escalates to the grievance authority.

The critical design decision: **an LLM is never the reason a rejection is
called wrongful.** The agent re-runs the deterministic rule engine against the
household and the rule version in force when the application was filed. If the
engine says eligible and the department says no, that contradiction is the
ground of appeal, and it is citable clause by clause. The model only turns that
finding into a letter.

This ordering is what makes "recovered one wrongly denied benefit" a claim you
can defend at a counter rather than a number on a slide.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from pydantic import BaseModel

from yogya.audit import entry
from yogya.data.loader import SchemeCorpus
from yogya.models.application import (
    Appeal,
    Application,
    ApplicationStatus,
    AuditEntry,
)
from yogya.models.household import Household
from yogya.models.scheme import EligibilityVerdict
from yogya.rules.engine import evaluate_scheme

AGENT_NAME = "grievance_escalation"

ASSESS_SYSTEM = """\
You assess whether a welfare application rejection is defensible.

You are given: the department's stated reason, and the output of an independent
re-evaluation of the family against the scheme's own published criteria.

Judge ONLY on whether the stated reason is contradicted by the re-evaluation.
Do not speculate about facts you were not given. If the re-evaluation says the
family is eligible and the department says otherwise, that is a contradiction.
"""

DRAFT_SYSTEM = """\
You draft a first-appeal letter against a welfare application rejection in India.

Style: formal, respectful, short. Address the appellate authority. State the
reference number, the scheme, the date of rejection, and the stated ground.
Then set out the grounds of appeal, citing the scheme clause by name exactly as
given to you.

Never cite a clause you were not given. Never assert a fact not in the input.
Never threaten. End by requesting reconsideration and, in the alternative, a
written speaking order.
"""


class AppealAssessmentOutput(BaseModel):
    assessed_as_wrongful: bool
    grounds: str
    confidence: float = 0.5


class GrievanceEscalationAgent:
    """Agent 6 of 7."""

    name = AGENT_NAME

    def __init__(self, llm, corpus: SchemeCorpus) -> None:
        self.llm = llm
        self.corpus = corpus

    # ---- assessment -------------------------------------------------------
    def assess(
        self, household: Household, application: Application
    ) -> tuple[Application, AuditEntry, bool]:
        """Re-evaluate the rejection. Returns (application, audit, is_wrongful)."""
        if application.rejection is None:
            raise ValueError(
                f"{application.application_id} has no rejection to assess"
            )

        scheme = self.corpus.get(
            application.scheme_id, version=application.rule_version
        )
        recheck = evaluate_scheme(household, scheme)
        engine_says_eligible = recheck.verdict is EligibilityVerdict.ELIGIBLE

        # The model is asked only after the engine has already decided. The
        # marker below is what the offline heuristic keys on; with a real
        # provider the model reads the same evidence in prose.
        marker = "contradicted_by_rules" if engine_says_eligible else "consistent"
        try:
            assessment = self.llm.structured(
                system=ASSESS_SYSTEM,
                user=(
                    f"Scheme: {scheme.name} (rule version {scheme.rule_version})\n"
                    f"Department's stated reason: {application.rejection.text}\n"
                    f"Independent re-evaluation verdict: {recheck.verdict.value} "
                    f"[{marker}]\n"
                    "Criteria:\n"
                    + "\n".join(
                        f"- [{'PASS' if r.passed else 'FAIL'}] {r.description} "
                        f"(cited: {r.citation})"
                        for r in recheck.results
                    )
                ),
                schema=AppealAssessmentOutput,
            )
            model_grounds = assessment.grounds
        except Exception:  # noqa: BLE001
            model_grounds = ""

        # The engine, not the model, is authoritative.
        is_wrongful = engine_says_eligible
        application.rejection.assessed_as_wrongful = is_wrongful
        application.rejection.assessment_note = (
            model_grounds
            or (
                "Independent re-evaluation against the scheme's published criteria "
                f"returns {recheck.verdict.value}."
            )
        )
        application.touch()

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action=(
                "assessed rejection as wrongful"
                if is_wrongful
                else "assessed rejection as defensible"
            ),
            detail=(
                f"Department said: {application.rejection.text}. "
                f"Re-evaluation at rule version {scheme.rule_version}: "
                f"{recheck.verdict.value}."
            ),
            scheme_id=application.scheme_id,
            application_id=application.application_id,
            citations=recheck.citations,
            rule_version=scheme.rule_version,
        )
        return application, audit, is_wrongful

    # ---- appeal drafting --------------------------------------------------
    def draft_appeal(
        self, household: Household, application: Application
    ) -> tuple[Appeal, AuditEntry]:
        scheme = self.corpus.get(
            application.scheme_id, version=application.rule_version
        )
        recheck = evaluate_scheme(household, scheme)
        passed = [r for r in recheck.results if r.passed and not r.unknown]

        grounds = (
            application.rejection.assessment_note
            if application.rejection
            else "Rejection reviewed against the scheme's published criteria."
        )

        user = (
            f"Scheme: {scheme.name}\n"
            f"Appellate authority: the authority designated under {scheme.name}\n"
            f"Reference number: {application.reference_number or 'not issued'}\n"
            f"Applicant: {household.head_name}, "
            f"{household.district or household.state}\n"
            f"Stated ground of rejection: "
            f"{application.rejection.text if application.rejection else 'not stated'}\n"
            f"Attempt number: {application.appeal_attempts + 1}\n"
            "Criteria the applicant demonstrably satisfies:\n"
            + "\n".join(f"- {r.description} (cited: {r.citation})" for r in passed)
        )
        try:
            letter = self.llm.text(system=DRAFT_SYSTEM, user=user)
        except Exception:  # noqa: BLE001
            letter = _template_letter(household, application, scheme, passed)
        if not letter.strip():
            letter = _template_letter(household, application, scheme, passed)

        appeal = Appeal(
            appeal_id=f"appeal-{uuid.uuid4().hex[:8]}",
            attempt=application.appeal_attempts + 1,
            grounds=grounds,
            letter_text=letter,
            citations=[r.citation for r in passed],
        )

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action=f"drafted appeal (attempt {appeal.attempt})",
            detail=f"{scheme.name}: {len(appeal.citations)} clause(s) cited",
            scheme_id=application.scheme_id,
            application_id=application.application_id,
            citations=appeal.citations,
            rule_version=scheme.rule_version,
        )
        return appeal, audit

    def record_appeal_filed(
        self, application: Application, appeal: Appeal
    ) -> Application:
        appeal.filed_at = datetime.now(timezone.utc)
        application.appeals.append(appeal)
        application.appeal_attempts += 1
        application.status = ApplicationStatus.APPEALED
        application.touch()
        return application

    # ---- escalation -------------------------------------------------------
    def escalate(
        self, household: Household, application: Application, reason: str
    ) -> tuple[Application, AuditEntry]:
        """Final step: hand off to the statutory grievance channel."""
        scheme = self.corpus.get(application.scheme_id)
        application.status = ApplicationStatus.ESCALATED
        application.touch()

        audit = entry(
            household_id=household.household_id,
            actor=self.name,
            action="escalated to grievance authority",
            detail=(
                f"{scheme.name}: {reason}. "
                f"Channel: {scheme.grievance_url or 'district grievance officer'}. "
                f"Appeals exhausted after {application.appeal_attempts} attempt(s)."
            ),
            scheme_id=application.scheme_id,
            application_id=application.application_id,
            rule_version=application.rule_version,
        )
        return application, audit


def _template_letter(household, application, scheme, passed) -> str:
    today = date.today().strftime("%d %B %Y")
    clauses = "\n".join(f"    {i + 1}. {r.description} — {r.citation}" for i, r in enumerate(passed))
    ground = application.rejection.text if application.rejection else "not stated"
    return f"""To,
The Appellate Authority,
{scheme.name}

Date: {today}
Subject: Appeal against rejection of application {application.reference_number or ''}

Respected Sir/Madam,

I, {household.head_name}, resident of {household.district or household.state},
applied under {scheme.name}. My application was rejected on the ground that:
"{ground}"

I respectfully submit that this ground is not sustainable. On the scheme's own
published criteria, my household satisfies the following requirements:

{clauses}

I therefore request that the rejection be reconsidered and the benefit
sanctioned. In the alternative, I request a written speaking order recording
the specific criterion said to be unmet, so that I may remedy it.

Yours faithfully,
{household.head_name}
"""
