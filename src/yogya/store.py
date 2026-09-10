"""Long-lived household state.

A benefits case is not a session. A family profiled in March has an income
certificate in April, an application rejected in June, and an appeal heard in
August — and the social worker who opens the case in August is often not the
one who opened it in March.

So the household profile, its applications and its audit trail live in the
LangGraph `Store` (namespaced per household), separate from the checkpointer,
which only holds the state of a single in-flight run. Restarting the process
loses the run; it must never lose the case.

`InMemoryStore` is the default so the project runs with no infrastructure. The
same code works against a Postgres-backed store by passing a different store
into `build_graph()` — nothing here assumes memory.
"""

from __future__ import annotations

from typing import Any

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from yogya.models.application import Application, AuditEntry
from yogya.models.household import Household

NAMESPACE = ("yogya", "households")

PROFILE_KEY = "profile"
APPLICATIONS_KEY = "applications"
AUDIT_KEY = "audit"


def namespace_for(household_id: str) -> tuple[str, ...]:
    return (*NAMESPACE, household_id)


class CaseStore:
    """Typed façade over the LangGraph store.

    Kept thin deliberately: the graph nodes should read and write cases in
    domain terms, not juggle namespaces and JSON.
    """

    def __init__(self, store: BaseStore | None = None) -> None:
        self.store: BaseStore = store or InMemoryStore()

    # ---- profile ----------------------------------------------------------
    def save_profile(self, household: Household) -> None:
        self.store.put(
            namespace_for(household.household_id),
            PROFILE_KEY,
            household.model_dump(mode="json"),
        )

    def load_profile(self, household_id: str) -> Household | None:
        item = self.store.get(namespace_for(household_id), PROFILE_KEY)
        if item is None:
            return None
        return Household.model_validate(item.value)

    # ---- applications -----------------------------------------------------
    def save_applications(
        self, household_id: str, applications: list[Application]
    ) -> None:
        """Merge by application_id so a partial run never drops history."""
        existing = {a.application_id: a for a in self.load_applications(household_id)}
        for app in applications:
            existing[app.application_id] = app
        self.store.put(
            namespace_for(household_id),
            APPLICATIONS_KEY,
            {"items": [a.model_dump(mode="json") for a in existing.values()]},
        )

    def load_applications(self, household_id: str) -> list[Application]:
        item = self.store.get(namespace_for(household_id), APPLICATIONS_KEY)
        if item is None:
            return []
        return [Application.model_validate(a) for a in item.value.get("items", [])]

    # ---- audit ------------------------------------------------------------
    def append_audit(self, household_id: str, entries: list[AuditEntry]) -> None:
        existing = self.load_audit(household_id)
        combined = existing + entries
        self.store.put(
            namespace_for(household_id),
            AUDIT_KEY,
            {"items": [e.model_dump(mode="json") for e in combined]},
        )

    def load_audit(self, household_id: str) -> list[AuditEntry]:
        item = self.store.get(namespace_for(household_id), AUDIT_KEY)
        if item is None:
            return []
        return [AuditEntry.model_validate(e) for e in item.value.get("items", [])]

    # ---- whole case -------------------------------------------------------
    def load_case(self, household_id: str) -> dict[str, Any]:
        return {
            "household": self.load_profile(household_id),
            "applications": self.load_applications(household_id),
            "audit": self.load_audit(household_id),
        }

    def known_fields(self, household_id: str) -> dict[str, Any]:
        """Confirmed prior values, handed to the profiler on a repeat visit.

        This is what turns the second intake into "what changed?" rather than
        the same twenty questions again.
        """
        profile = self.load_profile(household_id)
        if profile is None:
            return {}
        confirmed = profile.model_dump(
            mode="json",
            exclude={
                "household_id",
                "members",
                "documents",
                "intake_notes",
                "profiled_on",
                "unknown_fields",
            },
        )
        return {
            k: v
            for k, v in confirmed.items()
            if v is not None and v != "unknown" and k not in profile.unknown_fields
        }

    def households(self) -> list[str]:
        return sorted(
            {
                item.namespace[-1]
                for item in self.store.search(NAMESPACE, limit=1000)
                if len(item.namespace) > len(NAMESPACE)
            }
        )
