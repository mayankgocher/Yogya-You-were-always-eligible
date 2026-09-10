#!/usr/bin/env python
"""Fetch a fresh scheme snapshot from myscheme.gov.in.

    python scripts/ingest_myscheme.py --limit 50
    python scripts/ingest_myscheme.py --state "Uttar Pradesh"

Writes `<snapshot>.pending.json` into the corpus directory. Pending files are
NOT loaded by the running system: `SchemeCorpus.from_dir` only reads `*.json`,
and a pending file ends in `.pending.json`. Promote one with
`scripts/review_ingest.py` after checking the proposed criteria by hand.

Note: this needs outbound access to api.myscheme.gov.in. Many CI environments
and sandboxes block it; that is why the seed corpus is checked in and why
nothing at runtime depends on this script having been run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yogya.config import get_settings  # noqa: E402
from yogya.ingest.myscheme import IngestError, ingest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="restrict to one state, e.g. 'Uttar Pradesh'")
    parser.add_argument("--limit", type=int, help="stop after N schemes")
    parser.add_argument("--out", type=Path, help="corpus directory to write into")
    parser.add_argument("--snapshot-id", help="override the snapshot id")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    out_dir = args.out or get_settings().corpus_dir
    try:
        report = ingest(
            out_dir=out_dir,
            state=args.state,
            limit=args.limit,
            snapshot_id=args.snapshot_id,
        )
    except IngestError as exc:
        print(f"ingestion failed: {exc}", file=sys.stderr)
        return 1

    print(report.summary())
    print(f"written to {report.written_to}")
    print()
    print(
        "This snapshot is NOT active. Review the proposed criteria, then run:\n"
        f"  python scripts/review_ingest.py {report.written_to}"
    )
    if report.skipped:
        print(f"\n{len(report.skipped)} skipped:")
        for scheme_id, reason in report.skipped[:20]:
            print(f"  {scheme_id}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
