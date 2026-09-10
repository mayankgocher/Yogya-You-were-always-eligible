#!/usr/bin/env python
"""Promote a reviewed ingest snapshot into the active corpus.

    python scripts/review_ingest.py src/yogya/data/corpus/myscheme-2026-09.pending.json

This is the human gate on scheme *rules*, the counterpart to the interrupt
gates on scheme *actions*. An auto-extracted criterion decides whether a family
is told they qualify, so it does not go live because a regex matched. Someone
reads it first.

The script walks each scheme, shows the source eligibility prose beside the
criteria that were proposed from it, and asks. Accepted schemes lose their
`needs_review` flag and are written to a plain `.json` file the corpus loader
will read; rejected ones are dropped.

`--check` runs non-interactively and just reports what is pending, which is
what CI uses to fail a build that ships unreviewed rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yogya.models.scheme import Scheme  # noqa: E402


def show(scheme: dict) -> None:
    print("=" * 78)
    print(f"{scheme['name']}  [{scheme['scheme_id']}]")
    print(f"  level   : {scheme['level']}  state: {scheme.get('state') or '-'}")
    print(f"  benefit : {scheme['benefit_summary'][:200]}")
    print(f"  source  : {scheme.get('source_url')}")
    print()
    print("  SOURCE ELIGIBILITY TEXT")
    text = scheme.get("raw_eligibility_text", "")
    for i in range(0, min(len(text), 900), 74):
        print(f"    {text[i:i + 74]}")
    print()
    print("  PROPOSED CRITERIA")
    for criterion in scheme["criteria"]:
        print(
            f"    - {criterion['field']} {criterion['op']} {criterion['value']!r}"
            f"\n      {criterion['description']}"
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pending", type=Path, help="a *.pending.json snapshot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report pending schemes and exit non-zero; do not prompt",
    )
    parser.add_argument(
        "--accept-all",
        action="store_true",
        help="promote everything without prompting (do not use for real rules)",
    )
    args = parser.parse_args()

    if not args.pending.exists():
        print(f"no such file: {args.pending}", file=sys.stderr)
        return 1

    payload = json.loads(args.pending.read_text(encoding="utf-8"))
    schemes = payload.get("schemes", [])
    pending = [s for s in schemes if s.get("needs_review")]

    if args.check:
        print(f"{len(pending)} scheme(s) awaiting review in {args.pending.name}")
        return 1 if pending else 0

    accepted: list[dict] = []
    for scheme in schemes:
        if not scheme.get("needs_review"):
            accepted.append(scheme)
            continue
        if args.accept_all:
            answer = "y"
        else:
            show(scheme)
            answer = input("  accept this scheme? [y/N/q] ").strip().lower()
        if answer == "q":
            break
        if answer != "y":
            continue

        record = {k: v for k, v in scheme.items() if k not in {"needs_review", "raw_eligibility_text"}}
        for i, criterion in enumerate(record["criteria"], start=1):
            criterion["criterion_id"] = f"{record['scheme_id']}-{i}"
            criterion["citation"] = criterion["citation"].replace(
                " (auto-extracted, NEEDS REVIEW)", " (reviewed)"
            )
        try:
            Scheme.model_validate(record)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! rejected — does not validate: {exc}")
            continue
        accepted.append(record)

    if not accepted:
        print("nothing accepted; active corpus unchanged")
        return 0

    target = args.pending.with_suffix("").with_suffix(".json")
    if target.name.endswith(".pending"):
        target = target.with_name(target.name.replace(".pending", ""))
    target.write_text(
        json.dumps(
            {**payload, "schemes": accepted, "note": payload.get("note", "") + " Reviewed."},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\npromoted {len(accepted)} scheme(s) into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
