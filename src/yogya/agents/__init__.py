"""The seven agents.

    1. HouseholdProfiler          notes            -> Household
    2. EntitlementMatcher         Household+Scheme -> EligibilityResult
    3. DocumentGapAgent           Household+Scheme -> [DocumentGap]
    4. FormFillerAgent            Household+Scheme -> form fields + flags
    5. ApplicationTrackerAgent    Application      -> submitted / polled
    6. GrievanceEscalationAgent   rejection        -> assessment, appeal, escalation
    7. interrupts                 every act        -> a human decision
"""

from yogya.agents.documents import DocumentGapAgent
from yogya.agents.forms import FormFillerAgent
from yogya.agents.grievance import GrievanceEscalationAgent
from yogya.agents.interrupts import (
    AppealDecision,
    InterruptKind,
    InterruptRequest,
    ProfileDecision,
    SchemeSelection,
    SubmissionDecision,
)
from yogya.agents.matcher import EntitlementMatcher
from yogya.agents.profiler import HouseholdProfiler
from yogya.agents.tracker import ApplicationTrackerAgent, SubmissionRefused

__all__ = [
    "AppealDecision",
    "ApplicationTrackerAgent",
    "DocumentGapAgent",
    "EntitlementMatcher",
    "FormFillerAgent",
    "GrievanceEscalationAgent",
    "HouseholdProfiler",
    "InterruptKind",
    "InterruptRequest",
    "ProfileDecision",
    "SchemeSelection",
    "SubmissionDecision",
    "SubmissionRefused",
]
