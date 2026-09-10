"""Driving the graph across interrupts.

The graph halts at every human gate. Something has to hold the thread id, read
the pending interrupt, and resume with a decision. In production that is the
API plus a social worker; in tests and the demo it is `AutoCaseworker`, which
answers gates from a policy rather than a screen.

`AutoCaseworker` is not a bypass. It resumes the same interrupts through the
same `Command(resume=...)` path a human would — it just decides mechanically.
That is what makes it usable as a test double *and* as the offline demo driver
without either lying about how the system behaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from langgraph.types import Command

from yogya.agents.interrupts import (
    AppealDecision,
    InterruptKind,
    ProfileDecision,
    SchemeSelection,
    SubmissionDecision,
)
from yogya.graph.build import RECOMMENDED_RECURSION_LIMIT


def run_config(thread_id: str, recursion_limit: int | None = None) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit or RECOMMENDED_RECURSION_LIMIT,
    }


def pending_interrupt(graph, config: dict) -> dict | None:
    """The interrupt the graph is currently waiting on, if any."""
    state = graph.get_state(config)
    interrupts = getattr(state, "interrupts", None) or ()
    if not interrupts:
        # Older/newer surface: tasks carry their own interrupts.
        interrupts = [i for task in state.tasks for i in (task.interrupts or ())]
    if not interrupts:
        return None
    value = interrupts[0].value
    return value if isinstance(value, dict) else {"value": value}


@dataclass
class AutoCaseworker:
    """A scripted stand-in for the human at the gates.

    Each policy is a callable taking the interrupt payload and returning the
    decision. Defaults approve everything approvable, which is the right
    default for a smoke test and the wrong one for a real deployment — hence
    the explicit name.
    """

    on_profile: Callable[[dict], ProfileDecision] | None = None
    on_selection: Callable[[dict], SchemeSelection] | None = None
    on_submission: Callable[[dict], SubmissionDecision] | None = None
    on_appeal: Callable[[dict], AppealDecision] | None = None
    seen: list[dict] = field(default_factory=list)
    max_steps: int = 200

    def decide(self, request: dict) -> Any:
        self.seen.append(request)
        kind = request.get("kind")

        if kind == InterruptKind.CONFIRM_PROFILE.value:
            decision = (
                self.on_profile(request) if self.on_profile else ProfileDecision()
            )
        elif kind == InterruptKind.SELECT_SCHEMES.value:
            if self.on_selection:
                decision = self.on_selection(request)
            else:
                decision = SchemeSelection(
                    file_scheme_ids=[
                        e["scheme_id"] for e in request["payload"].get("eligible", [])
                    ]
                )
        elif kind == InterruptKind.APPROVE_SUBMISSION.value:
            decision = (
                self.on_submission(request)
                if self.on_submission
                else SubmissionDecision(action="submit")
            )
        elif kind == InterruptKind.APPROVE_APPEAL.value:
            decision = (
                self.on_appeal(request) if self.on_appeal else AppealDecision(action="file")
            )
        else:
            raise ValueError(f"unhandled interrupt kind: {kind!r}")

        return decision.model_dump(mode="json")

    # ---- driving ----------------------------------------------------------
    def run(self, graph, payload: dict, config: dict) -> dict:
        """Run to completion, answering every gate. Returns the final state."""
        graph.invoke(payload, config)
        for _ in range(self.max_steps):
            request = pending_interrupt(graph, config)
            if request is None:
                break
            graph.invoke(Command(resume=self.decide(request)), config)
        else:
            raise RuntimeError(
                "AutoCaseworker exceeded max_steps — the graph is looping on an "
                "interrupt it never clears"
            )
        return graph.get_state(config).values

    def iter_gates(self, graph, payload: dict, config: dict) -> Iterator[dict]:
        """Yield each interrupt as it is reached, for step-by-step inspection."""
        graph.invoke(payload, config)
        while True:
            request = pending_interrupt(graph, config)
            if request is None:
                return
            yield request
            graph.invoke(Command(resume=self.decide(request)), config)


__all__ = ["AutoCaseworker", "pending_interrupt", "run_config"]
