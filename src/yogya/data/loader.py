"""Corpus loading and version resolution.

Scheme rules change. A pension income ceiling revised in April 2024 does not
retroactively make last year's rejection correct, so the corpus keeps every
version of a rule set and callers ask for the one in force on a given date.

The corpus is a directory of JSON files. Each file carries a `snapshot_id`;
the ingest pipeline writes new snapshots without destroying old ones.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from yogya.config import get_settings
from yogya.models.scheme import Scheme


class CorpusError(RuntimeError):
    pass


class SchemeCorpus:
    """An immutable, queryable collection of scheme rule versions."""

    def __init__(self, schemes: list[Scheme], snapshot_id: str = "unknown") -> None:
        if not schemes:
            raise CorpusError("corpus is empty")
        self._schemes = schemes
        self.snapshot_id = snapshot_id

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_dir(cls, directory: Path) -> "SchemeCorpus":
        directory = Path(directory)
        if not directory.is_dir():
            raise CorpusError(f"corpus directory not found: {directory}")

        # `*.pending.json` files are freshly ingested snapshots whose criteria
        # no human has checked yet. They live in the same directory so they are
        # easy to review and diff, but they must never be loaded — an
        # auto-extracted income ceiling deciding a real family's eligibility is
        # exactly the failure this project exists to avoid.
        files = sorted(
            path for path in directory.glob("*.json")
            if not path.name.endswith(".pending.json")
        )
        if not files:
            raise CorpusError(
                f"no reviewed corpus files in {directory} "
                "(pending ingests are ignored until promoted)"
            )

        schemes: list[Scheme] = []
        snapshot_ids: set[str] = set()
        for path in files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = payload.get("snapshot_id", path.stem)
            snapshot_ids.add(snapshot)
            for raw in payload.get("schemes", []):
                raw.setdefault("source_snapshot", snapshot)
                if payload.get("state") and not raw.get("state"):
                    raw["state"] = payload["state"]
                schemes.append(Scheme.model_validate(raw))

        cls._assert_no_duplicate_versions(schemes)
        return cls(schemes, snapshot_id=",".join(sorted(snapshot_ids)))

    @staticmethod
    def _assert_no_duplicate_versions(schemes: list[Scheme]) -> None:
        seen: set[tuple[str, str]] = set()
        for s in schemes:
            key = (s.scheme_id, s.rule_version)
            if key in seen:
                raise CorpusError(
                    f"duplicate rule version {s.rule_version} for scheme {s.scheme_id}"
                )
            seen.add(key)

    # ---- queries ----------------------------------------------------------
    @property
    def all_versions(self) -> list[Scheme]:
        return list(self._schemes)

    def current(self, on: date | None = None) -> list[Scheme]:
        """One Scheme per scheme_id: the version in force on `on` (today by default).

        A version is in force when it has started (effective_from <= on) and has
        not been superseded (superseded_on is None or >= on). When several
        qualify — sloppy corpus data — the latest effective_from wins.
        """
        as_of = on or date.today()
        best: dict[str, Scheme] = {}
        for s in self._schemes:
            if s.effective_from and s.effective_from > as_of:
                continue
            if s.superseded_on and s.superseded_on < as_of:
                continue
            incumbent = best.get(s.scheme_id)
            if incumbent is None:
                best[s.scheme_id] = s
                continue
            if (s.effective_from or date.min) > (incumbent.effective_from or date.min):
                best[s.scheme_id] = s
        return sorted(best.values(), key=lambda s: s.scheme_id)

    def get(self, scheme_id: str, version: str | None = None) -> Scheme:
        """Fetch one scheme, at a specific rule version if asked."""
        candidates = [s for s in self._schemes if s.scheme_id == scheme_id]
        if not candidates:
            raise KeyError(f"unknown scheme: {scheme_id}")
        if version is None:
            current = [s for s in self.current() if s.scheme_id == scheme_id]
            if current:
                return current[0]
            return max(candidates, key=lambda s: s.effective_from or date.min)
        for s in candidates:
            if s.rule_version == version:
                return s
        raise KeyError(f"scheme {scheme_id} has no rule version {version}")

    def for_household(self, state: str, on: date | None = None) -> list[Scheme]:
        """Central schemes plus those of the household's own state.

        Filtering by state before eligibility is not an optimisation — a
        Bihar family is not merely ineligible for a UP pension, the scheme
        does not apply to them at all, and surfacing it as 'rejected' would be
        misleading in the audit trail.
        """
        return [
            s
            for s in self.current(on)
            if s.level == "central" or s.state == state
        ]

    def versions_of(self, scheme_id: str) -> list[str]:
        return sorted(
            s.rule_version for s in self._schemes if s.scheme_id == scheme_id
        )

    def __len__(self) -> int:
        return len(self.current())


@lru_cache(maxsize=4)
def load_corpus(directory: Path | None = None) -> SchemeCorpus:
    """Process-wide cached corpus load."""
    target = Path(directory) if directory else get_settings().corpus_dir
    return SchemeCorpus.from_dir(target)


def clear_corpus_cache() -> None:
    load_corpus.cache_clear()
