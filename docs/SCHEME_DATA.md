# Scheme data

## Format

One JSON file per corpus slice, in `src/yogya/data/corpus/`.

```jsonc
{
  "snapshot_id": "seed-2026-09",
  "level": "state",
  "state": "uttar_pradesh",      // inherited by schemes that omit it
  "note": "provenance and caveats",
  "schemes": [ /* Scheme records */ ]
}
```

A scheme:

```jsonc
{
  "scheme_id": "nsap-ignoaps",
  "name": "Indira Gandhi National Old Age Pension Scheme (IGNOAPS)",
  "level": "central",                    // central | state | local
  "state": null,                          // null for central
  "benefit_kind": "pension",
  "benefit_summary": "Monthly old-age pension…",
  "benefit_value_annual_inr": 7200,       // indicative estimate
  "criteria": [
    {
      "criterion_id": "ignoaps-age",
      "field": "max_age",                 // must exist in Household.rule_context()
      "op": "gte",
      "value": 60,
      "description": "At least one member must be aged 60 or above.",
      "citation": "NSAP Guidelines, cl. 3.1",
      "remediable": false,
      "unknown_is_blocking": true
    }
  ],
  "required_documents": ["aadhaar", "ration_card", "bank_passbook"],
  "rule_version": "1.0.0",
  "effective_from": "2007-11-19",
  "superseded_on": null,
  "source_url": "https://nsap.nic.in/"
}
```

### Field rules

- **`field`** must be a key of `Household.rule_context()`. A test enforces this
  across the whole corpus — a typo would otherwise silently disable a criterion.
- **`citation`** is not optional. It is what a caseworker reads out at a
  counter. A criterion without one is a claim we cannot defend.
- **`remediable`** means the family can fix it — open a bank account, get a
  certificate. Remediable failures still produce `NOT_ELIGIBLE`, but they are
  surfaced as an errand rather than a dead end.
- **`unknown_is_blocking`** decides what happens when the field is unknown.
  Use `true` where the criterion is the scheme's whole point (a widow pension
  needs to know there is a widow); `false` where an unknown should merely warn.
- **`benefit_value_annual_inr`** for an insurance scheme is a *cover* amount and
  is never counted as cash recovered — see ADR-009.
- **`required_documents`** must all appear in `DOCUMENT_GUIDANCE`
  (`agents/documents.py`), so we can always say where to get one. A test
  enforces it.

## Versioning

Rules change. An income ceiling revised in April 2024 does not retroactively
make last year's rejection correct.

Several records may share a `scheme_id`, distinguished by `rule_version`, and
bounded by `effective_from` / `superseded_on`. The corpus ships one live
example: `up-old-age-pension` at 1.0.0 (ceiling ₹46,080, superseded
2024-03-31) and 1.1.0 (₹56,460, effective 2024-04-01).

```python
corpus.current()                    # the version in force today
corpus.current(date(2023, 6, 1))    # the version in force then
corpus.get("up-old-age-pension", version="1.0.0")
corpus.versions_of("up-old-age-pension")
```

Every `EligibilityResult` and every audit entry records its `rule_version`, and
`Application.rule_version` pins the rules an application was filed under.
`GrievanceEscalationAgent.assess` re-evaluates against *that* version — using
today's rules would manufacture wrongful denials that never happened.

`build_graph(as_of=...)` sets the date for a whole run.

## What ships

22 current schemes (23 records including the superseded pension).

**Central (17):** PM-KISAN · PM-JAY · IGNOAPS · IGNWPS · IGNDPS · PMAY-G ·
PMUY · NFSA priority entitlement · PMMVY · Sukanya Samriddhi · PM SVANidhi ·
PM Vishwakarma · PMJJBY · PMSBY · pre-matric SC scholarship · post-matric OBC
scholarship · MGNREGS

**Uttar Pradesh (5 current):** old age pension · destitute women pension ·
Divyangjan maintenance · Kanya Sumangala · BOCW welfare board assistance

Values are indicative and **must be re-verified against the current
notification before any real-world use**. This is stated in every corpus file's
`note` field, not just here.

## Ingestion from myScheme

```bash
python scripts/ingest_myscheme.py --limit 50
python scripts/ingest_myscheme.py --state "Uttar Pradesh"
```

Writes `<snapshot>.pending.json`. Two invariants:

**Nothing at runtime ever calls myScheme.** Ingestion produces a snapshot on
disk. A portal outage, a layout change or a rate limit can never take Yogya
down or change a verdict mid-case. The snapshot a decision was made against is
recorded in the audit trail.

**Auto-extracted criteria are not live.** `propose_criteria()` reads eligibility
prose and proposes structured criteria, each cited as
`… (auto-extracted, NEEDS REVIEW)`. Every scheme carries `needs_review: true`.
`SchemeCorpus.from_dir` skips `*.pending.json` entirely.

Promote after reading:

```bash
python scripts/review_ingest.py src/yogya/data/corpus/myscheme-2026-09.pending.json
python scripts/review_ingest.py <file> --check    # CI: non-zero if anything is pending
```

The reviewer sees the source prose beside the proposed criteria and accepts or
rejects each scheme. Accepted schemes lose the flag, get stable criterion ids,
and are written to a plain `.json` the loader will read.

### What the proposer can and cannot read

Handles: income ceilings including lakh/crore units · minimum and maximum age ·
SC/ST/OBC/EWS category · BPL and Antyodaya language · widow · disability ·
rural/urban (only when unambiguous) · income-tax exclusion.

Cannot handle, and will silently miss: per-member versus per-household income ·
"one of the following" disjunctions · state-specific amendments · land units in
bigha or katha · anything expressed as a table.

That gap is the reason for the review gate. A scheme where nothing could be
proposed is skipped and reported rather than admitted with no criteria — a
scheme with an empty criteria list would report everyone eligible.

## Adding a scheme by hand

1. Find the notification. Not a news article, not a summary blog.
2. Add a record to the right corpus file with `rule_version: "1.0.0"`.
3. One `Criterion` per condition, each citing its clause.
4. Set `remediable` and `unknown_is_blocking` deliberately.
5. Ensure every `required_documents` entry exists in `DOCUMENT_GUIDANCE`.
6. Run `pytest tests/unit/test_corpus.py` — it checks field names, citations,
   document coverage and duplicate versions.
7. Run the demo. If the new scheme changes the story, update
   `tests/integration/test_demo.py` deliberately rather than loosening it.
