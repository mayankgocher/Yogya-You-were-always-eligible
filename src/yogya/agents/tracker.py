"""Application Tracker agent.

Submits an approved application and, later, reads its status back.

Two things make this more than a wrapper around the portal client:

* It is the only component permitted to move an Application into SUBMITTED,
  and it refuses to do so if the form still has flagged fields or blocking
  document gaps. The human interrupt is the *policy* gate; this is the
  *mechanical* one, and both must pass.
* Status changes are recorded with timestamps so that "you have been waiting
  47 days, the scheme's own service standard is 30" is answerable — which is
  what a grievance actually needs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from yogya.audit import entry
from yogya.models.application import (
    Application,
    ApplicationStatus,
    AuditEntry,
    RejectionReason,
)
from yogya.portal import PortalGateway, PortalResponse

AGENT_NAME = "application_tracker"


class SubmissionRefused(RuntimeError):
    """Raised when the tracker is asked to submit something unfit to submit."""


class ApplicationTrackerAgent:
    """Agent 5 of 7."""

    name = AGENT_NAME

    def __init__(self, portal: PortalGateway) -> None:
        self.portal = portal

    # ---- submission -------------------------------------------------------
    def submit(self, application: Application) -> tuple[Application, AuditEntry]:
        self._assert_submittable(application)

        response = self.portal.submit(
            application.application_id, application.scheme_id, application.form_data
        )
        application.status = (
            ApplicationStatus.SUBMITTED if response.accepted else ApplicationStatus.DRAFT
        )
        application.reference_number = response.reference_number
        application.submitted_at = datetime.now(timezone.utc)
        application.touch()

        audit = entry(
            household_id=application.household_id,
            actor=self.name,
            action="submitted application",
            detail=(
                f"{application.scheme_name}: reference "
                f"{application.reference_number or 'not issued'}"
            ),
            scheme_id=application.scheme_id,
            application_id=application.application_id,
            rule_version=application.rule_version,
        )
        return application, audit

    def _assert_submittable(self, application: Application) -> None:
        from yogya.agents.forms import FormFillerAgent

        if application.blocking_gaps:
            missing = ", ".join(g.document.value for g in application.blocking_gaps)
            raise SubmissionRefused(
                f"{application.application_id}: blocking documents missing: {missing}"
            )
        if not FormFillerAgent.is_submittable(application.form_data):
            flagged = ", ".join(FormFillerAgent.flagged_fields(application.form_data))
            raise SubmissionRefused(
                f"{application.application_id}: form fields need a human: {flagged}"
            )

    # ---- status -----------------------------------------------------------
    def poll(self, application: Application) -> tuple[Application, AuditEntry]:
        # The attempt number is the appeal count: a poll after the first appeal
        # is a different question than the original decision, and the portal is
        # entitled to answer it differently.
        response: PortalResponse = self.portal.poll(
            application.application_id,
            application.scheme_id,
            attempt=application.appeal_attempts,
        )

        if response.status == "approved":
            application.status = ApplicationStatus.APPROVED
            application.decided_at = datetime.now(timezone.utc)
            detail = f"{application.scheme_name}: approved"
        elif response.status == "rejected":
            application.status = ApplicationStatus.REJECTED
            application.decided_at = datetime.now(timezone.utc)
            application.rejection = RejectionReason(
                code=response.rejection_code or "UNSPECIFIED",
                text=response.rejection_text or "No reason recorded by the portal.",
            )
            detail = (
                f"{application.scheme_name}: rejected "
                f"({application.rejection.code}) — {application.rejection.text}"
            )
        else:
            application.status = ApplicationStatus.WAITING
            detail = f"{application.scheme_name}: still pending with the department"

        application.touch()
        audit = entry(
            household_id=application.household_id,
            actor=self.name,
            action=f"polled status: {application.status.value}",
            detail=detail,
            scheme_id=application.scheme_id,
            application_id=application.application_id,
        )
        return application, audit

    # ---- service standards ------------------------------------------------
    @staticmethod
    def days_waiting(application: Application, now: datetime | None = None) -> int:
        if not application.submitted_at:
            return 0
        end = application.decided_at or now or datetime.now(timezone.utc)
        return max((end - application.submitted_at).days, 0)

    @staticmethod
    def is_overdue(application: Application, service_standard_days: int = 30) -> bool:
        """Undecided past the department's own service standard.

        Being overdue is itself a ground for grievance, separate from being
        rejected — a family left waiting indefinitely has been denied in
        practice without anyone having to write it down.
        """
        if application.status not in {
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.WAITING,
        }:
            return False
        return (
            ApplicationTrackerAgent.days_waiting(application) > service_standard_days
        )
