"""Government portal gateway.

Yogya never submits to a real government portal in this build, and the seam
where it would is deliberately narrow and explicit.

`PortalGateway` is the protocol. `SimulatedPortal` is the only implementation
shipped, and it is fully deterministic: given the same application id it
returns the same outcome every time, which is what makes the demo reproducible
and the rejection-loop tests meaningful.

`poll()` takes an `attempt` number so that the outcome can legitimately change
after an appeal has been filed — which is the entire point of the appeal path.
Scripted outcomes are therefore a *sequence* per application: index 0 is the
original decision, index 1 the decision after the first appeal, and so on, with
the last entry repeating.

A real implementation would go here, behind the same two methods — and would
still be unreachable until a human has passed the `interrupt()` gate in the
application subgraph.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PortalResponse:
    accepted: bool
    reference_number: str | None = None
    status: str = "waiting"  # "waiting" | "approved" | "rejected"
    rejection_code: str | None = None
    rejection_text: str | None = None


class PortalGateway(Protocol):
    def submit(self, application_id: str, scheme_id: str, form: dict) -> PortalResponse:
        ...

    def poll(
        self, application_id: str, scheme_id: str, attempt: int = 0
    ) -> PortalResponse:
        ...


@dataclass
class SimulatedPortal:
    """Deterministic portal stand-in.

    `scripted` maps application_id -> outcomes by attempt, and is how tests and
    the demo scenario pin down exactly which applications come back rejected
    and which are overturned on appeal. Anything unscripted falls back to a
    stable hash of the id, so unscripted runs still produce a realistic mix
    rather than an implausible clean sweep.
    """

    scripted: dict[str, list[PortalResponse]] = field(default_factory=dict)
    rejection_rate: float = 0.35
    submissions: list[tuple[str, str]] = field(default_factory=list)
    polls: list[tuple[str, int]] = field(default_factory=list)

    def submit(self, application_id: str, scheme_id: str, form: dict) -> PortalResponse:
        self.submissions.append((application_id, scheme_id))
        return PortalResponse(
            accepted=True,
            reference_number=self._reference(application_id),
            status="waiting",
        )

    def poll(
        self, application_id: str, scheme_id: str, attempt: int = 0
    ) -> PortalResponse:
        self.polls.append((application_id, attempt))
        sequence = self.scripted.get(application_id)
        if sequence:
            index = min(attempt, len(sequence) - 1)
            return sequence[index]
        if self._unit_hash(application_id) < self.rejection_rate:
            return PortalResponse(
                accepted=True,
                reference_number=self._reference(application_id),
                status="rejected",
                rejection_code="INCOMPLETE_DOCS",
                rejection_text=(
                    "Application rejected: supporting documents found incomplete "
                    "at preliminary scrutiny."
                ),
            )
        return PortalResponse(
            accepted=True,
            reference_number=self._reference(application_id),
            status="approved",
        )

    # ---- scripting helpers ------------------------------------------------
    def script(self, application_id: str, *outcomes: PortalResponse) -> None:
        self.scripted[application_id] = list(outcomes)

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _unit_hash(cls, value: str) -> float:
        return int(cls._digest(value)[:8], 16) / 0xFFFFFFFF

    @classmethod
    def _reference(cls, application_id: str) -> str:
        return f"REF-{cls._digest(application_id)[:10].upper()}"


def approved(reference: str = "REF-OK") -> PortalResponse:
    return PortalResponse(accepted=True, reference_number=reference, status="approved")


def rejected(text: str, code: str = "CRITERIA_NOT_MET") -> PortalResponse:
    """A rejection. Whether it is *wrongful* is decided by the rule engine.

    Nothing in this text determines that — the grievance agent re-evaluates the
    household against the scheme's own criteria and compares.
    """
    return PortalResponse(
        accepted=True,
        reference_number="REF-REJ",
        status="rejected",
        rejection_code=code,
        rejection_text=text,
    )


def still_waiting() -> PortalResponse:
    return PortalResponse(accepted=True, reference_number="REF-WAIT", status="waiting")
