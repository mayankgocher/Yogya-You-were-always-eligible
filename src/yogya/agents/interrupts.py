"""Social Worker Interrupt agent.

Not really an agent — a policy, expressed as typed payloads.

"The social worker is always the boss" is only true if it is structural. So
every point where Yogya would act on a family's behalf raises a LangGraph
`interrupt()` carrying a typed request, the graph halts, and it cannot resume
without an explicit decision. There is no configuration that submits an
application to a government portal without a human having seen the form.

`require_human_approval=False` exists for tests and only downgrades the
*review* gates (profile confirmation, scheme selection). The submission gate is
unconditional — see `SUBMISSION_GATE_IS_MANDATORY`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

AGENT_NAME = "social_worker_interrupt"

# Read by the subgraph. Named loudly so that anyone tempted to add a bypass
# flag has to delete this line and explain themselves in the diff.
SUBMISSION_GATE_IS_MANDATORY = True


class InterruptKind(str, Enum):
    CONFIRM_PROFILE = "confirm_profile"
    SELECT_SCHEMES = "select_schemes"
    APPROVE_SUBMISSION = "approve_submission"
    APPROVE_APPEAL = "approve_appeal"


class InterruptRequest(BaseModel):
    """What the graph shows the social worker when it pauses."""

    kind: InterruptKind
    household_id: str
    title: str
    body: str
    # Everything the UI needs to render an actionable card.
    payload: dict[str, Any] = Field(default_factory=dict)
    # What the human is allowed to do here.
    options: list[str] = Field(default_factory=list)
    scheme_id: str | None = None
    application_id: str | None = None


class ProfileDecision(BaseModel):
    action: Literal["confirm", "amend"] = "confirm"
    # Field -> corrected value. Applied over the extracted profile.
    corrections: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class SchemeSelection(BaseModel):
    file_scheme_ids: list[str] = Field(default_factory=list)
    already_receiving_scheme_ids: list[str] = Field(default_factory=list)
    note: str = ""


class SubmissionDecision(BaseModel):
    action: Literal["submit", "hold", "skip"] = "submit"
    # Human edits to the form, applied before submission.
    form_edits: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class AppealDecision(BaseModel):
    action: Literal["file", "edit_and_file", "abandon"] = "file"
    letter_text: str | None = None
    note: str = ""


# ---- request builders -----------------------------------------------------


def confirm_profile_request(household, unresolved: list[str]) -> InterruptRequest:
    return InterruptRequest(
        kind=InterruptKind.CONFIRM_PROFILE,
        household_id=household.household_id,
        title=f"Confirm the profile for {household.head_name}",
        body=(
            "These fields could not be determined from the intake notes and "
            "change which schemes this family sees: "
            + ", ".join(unresolved)
            if unresolved
            else "Confirm the extracted profile before screening."
        ),
        payload={
            "household": household.model_dump(mode="json"),
            "unresolved_fields": unresolved,
        },
        options=["confirm", "amend"],
    )


def select_schemes_request(household_id: str, eligible, needs_info) -> InterruptRequest:
    return InterruptRequest(
        kind=InterruptKind.SELECT_SCHEMES,
        household_id=household_id,
        title=f"{len(eligible)} benefits found — choose what to file",
        body=(
            "Tick the schemes to file now, and mark any the family already "
            "receives so they are not filed twice."
        ),
        payload={
            "eligible": [e.model_dump(mode="json") for e in eligible],
            "needs_info": [e.model_dump(mode="json") for e in needs_info],
        },
        options=["file_selected"],
    )


def approve_submission_request(application, scheme_name: str) -> InterruptRequest:
    return InterruptRequest(
        kind=InterruptKind.APPROVE_SUBMISSION,
        household_id=application.household_id,
        title=f"Approve submission — {scheme_name}",
        body=(
            "Nothing is sent to a government portal until you approve it. "
            "Check every field, especially any marked as needing a human."
        ),
        payload={"application": application.model_dump(mode="json")},
        options=["submit", "hold", "skip"],
        scheme_id=application.scheme_id,
        application_id=application.application_id,
    )


def approve_appeal_request(application, appeal, scheme_name: str) -> InterruptRequest:
    return InterruptRequest(
        kind=InterruptKind.APPROVE_APPEAL,
        household_id=application.household_id,
        title=f"Approve appeal — {scheme_name}",
        body=(
            "This rejection was assessed against the scheme's published "
            "criteria. Read the draft before it is filed."
        ),
        payload={
            "application": application.model_dump(mode="json"),
            "appeal": appeal.model_dump(mode="json"),
        },
        options=["file", "edit_and_file", "abandon"],
        scheme_id=application.scheme_id,
        application_id=application.application_id,
    )
