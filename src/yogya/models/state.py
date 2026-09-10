"""LangGraph state schemas.

Two graphs, two states:

* `YogyaState` — the outer graph: one intake session for one household.
* `ApplicationState` — the per-application subgraph, one instance per scheme
  the social worker chose to file.

Reducers matter here. Eligibility checks fan out in parallel (map-reduce), so
`eligibility` and `audit` must be additive; a plain assignment would drop all
but the last parallel branch's write.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from yogya.models.application import Application, AuditEntry, RunMetrics
from yogya.models.household import Household
from yogya.models.scheme import EligibilityResult


def merge_applications(
    left: list[Application], right: list[Application]
) -> list[Application]:
    """Additive merge keyed by application_id, right wins on conflict.

    Parallel subgraph branches each return the single application they own;
    this folds them back into one list without clobbering siblings.
    """
    by_id: dict[str, Application] = {a.application_id: a for a in left}
    for app in right:
        by_id[app.application_id] = app
    return list(by_id.values())


def merge_metrics(left: RunMetrics, right: RunMetrics) -> RunMetrics:
    """Field-wise sum of counters, so parallel branches can each report."""
    data: dict[str, Any] = {}
    for name in RunMetrics.model_fields:
        data[name] = getattr(left, name, 0) + getattr(right, name, 0)
    return RunMetrics(**data)


class YogyaState(TypedDict, total=False):
    """Outer graph state for a single household session."""

    household_id: str
    intake_notes: str
    raw_profile: dict[str, Any]
    household: Household

    # Schemes retrieved as candidates, before eligibility is evaluated.
    candidate_scheme_ids: list[str]
    # Filled by the parallel map step; additive.
    eligibility: Annotated[list[EligibilityResult], operator.add]

    # Schemes the social worker approved for filing.
    selected_scheme_ids: list[str]
    # Schemes the family already receives — excluded and counted separately.
    already_receiving_scheme_ids: list[str]

    applications: Annotated[list[Application], merge_applications]
    audit: Annotated[list[AuditEntry], operator.add]
    metrics: Annotated[RunMetrics, merge_metrics]

    # Set when the run finished; the API surfaces this as the summary payload.
    summary: dict[str, Any]
    corpus_snapshot: str


class ApplicationState(TypedDict, total=False):
    """Per-application subgraph state.

    Keys are shared *by name* with the parent graph, which is how state flows
    in and out of the subgraph node without an adapter. Two consequences are
    easy to get wrong, and both cost real debugging time:

    **Never share an accumulator key with the parent.** If this schema declared
    `audit: Annotated[list, operator.add]` — the same name and reducer as the
    parent — the subgraph would be seeded with the parent's entire audit list,
    append to it, and return the whole thing; the parent's reducer would then
    append *that* to its own copy. The log doubles on every application, which
    looks like an exponential slowdown rather than a correctness bug. So the
    subgraph accumulates into its own `run_audit` / `run_metrics` channels and
    the parent folds them in once, in `archive`.

    **`eligibility` is deliberately absent.** In the parent that key holds the
    accumulated list for the whole sweep; reusing the name for a single result
    would silently shadow it. The grievance agent re-evaluates from the rule
    engine when it needs a verdict, which is the safer source anyway.
    """

    household: Household
    application: Application
    # Scratch space for the current attempt: the human's submission decision
    # and the draft appeal awaiting approval.
    portal_outcome: dict[str, Any]
    # Private accumulators, drained by the parent after each application.
    run_audit: Annotated[list[AuditEntry], operator.add]
    run_metrics: Annotated[RunMetrics, merge_metrics]


class OrchestratorState(YogyaState, total=False):
    """The graph's real state: YogyaState plus the application queue.

    Kept separate so that `YogyaState` remains the documented public contract
    while the queue machinery stays an implementation detail.
    """

    pending_applications: list[Application]
    current_application: Application | None
    application: Application | None
    portal_outcome: dict[str, Any]
    # Drain channels for the subgraph. No reducer here on purpose: the parent
    # overwrites them (cleared before each application, folded in after), while
    # the subgraph accumulates into them internally.
    run_audit: list[AuditEntry]
    run_metrics: RunMetrics
