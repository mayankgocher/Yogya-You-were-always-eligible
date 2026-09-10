"""Audit trail construction.

Every claim Yogya makes about a family — eligible, ineligible, wrongly denied —
must be reconstructible months later from this log alone. A benefits system
that cannot show its working is not usable by a social worker who has to defend
the claim at a block office counter.

Entries are append-only and carry the rule version and clause citations that
produced them.
"""

from __future__ import annotations

import itertools
from typing import Iterable

from yogya.models.application import AuditEntry

_counter = itertools.count(1)


def next_entry_id() -> str:
    return f"audit-{next(_counter):06d}"


def reset_entry_ids() -> None:
    """Tests call this so entry ids are stable across runs."""
    global _counter
    _counter = itertools.count(1)


def entry(
    *,
    household_id: str,
    actor: str,
    action: str,
    detail: str = "",
    scheme_id: str | None = None,
    application_id: str | None = None,
    citations: Iterable[str] | None = None,
    rule_version: str | None = None,
) -> AuditEntry:
    """Build one audit entry. The only sanctioned way to create them."""
    return AuditEntry(
        entry_id=next_entry_id(),
        household_id=household_id,
        actor=actor,
        action=action,
        detail=detail,
        scheme_id=scheme_id,
        application_id=application_id,
        citations=sorted(set(citations or ())),
        rule_version=rule_version,
    )


def render(entries: Iterable[AuditEntry]) -> str:
    """Human-readable trail, for the UI's 'show the working' panel."""
    lines: list[str] = []
    for e in entries:
        stamp = e.at.strftime("%Y-%m-%d %H:%M")
        head = f"[{stamp}] {e.actor} — {e.action}"
        if e.scheme_id:
            head += f" ({e.scheme_id}"
            head += f" @ {e.rule_version})" if e.rule_version else ")"
        lines.append(head)
        if e.detail:
            lines.append(f"    {e.detail}")
        for citation in e.citations:
            lines.append(f"    ├ cited: {citation}")
    return "\n".join(lines)
