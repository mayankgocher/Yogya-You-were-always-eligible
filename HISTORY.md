# HISTORY

A build log, kept for the reason build logs are worth keeping: so the next
person does not re-make a decision that was already made, or re-discover a bug
that was already expensive once.

Entries are newest-last. Each records what changed, why, and — where it
matters — what it cost.

---

## v1.0.0 — initial build

### Naming

Candidates considered: *Haqdaar*, *Paatra*, *Yogya*, *Samiti*, *Baaki*,
*Labharthi*.

- **Haqdaar** was dropped: [Haqdarshak](https://www.haqdarshak.com/) is an
  established company doing agent-assisted entitlement discovery at scale. The
  name would have read as an imitation.
- **Labharthi** was dropped: it is already Bihar's pension portal
  (e-Labharthi) and an MKCL e-governance product.
- **Yogya** (योग्य, "qualified") was chosen. It maps directly onto the framing
  question — *we didn't know we qualified* — and reads in both Hindi and
  English. Tagline: *"You were always eligible."*

Prior art is credited in the README rather than avoided. A reviewer who knows
the space will think of Haqdarshak within thirty seconds; better to name it
first and say what is different.

### Foundational decisions

**Rules decide, the model explains.** Taken before any code was written, and
everything else follows from it. See `docs/DECISIONS.md` ADR-001.

**Three-valued eligibility logic.** Added as soon as the first sparse profile
was tested. Two-valued logic forces a default for unknown income, and both
possible defaults are wrong in opposite directions.

**Deterministic rule engine over LLM-as-judge**, and **BM25 over embeddings**.
The corpus is a few hundred documents, not a few million; BM25 plus hard
structural filters wins on quality, costs nothing, needs no vector service on a
free tier, and — decisively — is inspectable. Retrieval was then configured to
be exhaustive rather than top-k: it *orders* candidates and never drops them,
because a scheme missed by a keyword is a family who never hears about it.

### Data

Hand-authored 22 central and Uttar Pradesh schemes with structured, cited
criteria. The UP old age pension is deliberately present twice — the pre-2024
income ceiling (₹46,080) and the current one (₹56,460) — so that version
resolution is exercised by real data rather than a synthetic fixture.

myScheme ingestion was built with the portal unreachable from the build
environment (the gateway denies `api.myscheme.gov.in`). Rather than fake it,
the pipeline was written against recorded payloads and tested with
`httpx.MockTransport`. This forced a better design than network access would
have: ingestion writes *snapshots*, nothing at runtime ever calls myScheme, and
auto-proposed criteria land in `*.pending.json` which the corpus loader refuses
to read until a human promotes them.

### Bugs worth remembering

**1. The application lifecycle silently never ran.**

`prepare_applications` wrote a queue of fifteen applications;
`next_application` read it as empty, routed to "done", and the graph finished
having filed nothing. No error, no warning.

Cause: LangGraph filters a node's input to the keys of its annotated state
schema. The nodes were typed `YogyaState`, which does not declare
`pending_applications`, so the queue was invisible to the very node that
consumed it. Every node in `build.py` is now annotated `OrchestratorState`.

*Lesson:* in LangGraph, a node's type annotation is not documentation — it is a
filter.

**2. The audit trail doubled on every application.**

Symptom: the demo ran fine for six applications and then took 26 seconds, then
67 seconds, for the next two. It looked like a performance problem.

Instrumenting state size found the real shape: audit entries went
63 → 283 → 1143 → 4583 → 18343 → 36695 → 587167. Exponential, not slow.

Cause: the lifecycle subgraph declared `audit: Annotated[list, operator.add]`
— the same key *and* the same reducer as the parent. As a subgraph node it was
seeded with the parent's entire audit list, appended its own entries, and
returned the whole thing; the parent's reducer then appended *that* to its own
copy. Each application therefore doubled the log.

Fix: the subgraph accumulates into private `run_audit` / `run_metrics`
channels. `next_application` clears them; `archive` folds them into the
run-wide channels exactly once. Guarded by
`test_audit_trail_does_not_duplicate` and
`test_the_subgraph_accumulates_into_its_own_channels`.

*Lesson:* never share an accumulator channel between a parent graph and a
subgraph. And a correctness bug can present purely as a performance one.

**3. Declaring checkpoint types turned models into dicts.**

Declaring an allowlist to LangGraph's serialiser silenced its warnings — and
broke everything, with `'dict' object has no attribute 'unknown_fields'` from
a node three steps away. The allowlist takes `(module, name)` tuples or
classes; single-element module tuples matched nothing, and an undeclared type
does not raise, it silently deserialises as a plain dict.

Fix: `YOGYA_CHECKPOINT_TYPES` lists the model classes explicitly, with a
docstring saying why the list must stay exhaustive.

### Design changes made during the build

**Applications run sequentially, not in parallel.** The original plan fanned
out application lifecycles with `Send`, matching the eligibility sweep. Two
problems: N simultaneous interrupts force the caller to resume by interrupt id,
and — more importantly — a caseworker approves one form at a time, so parallel
gates are worse for the actual user. Applications now run through the subgraph
via a queue and a loop edge. Eligibility, which has no interrupts, is still
genuinely parallel. Parallelism where it helps; absent where it would only look
impressive.

**Application ids became deterministic.** `app-{household_id}-{scheme_id}`
instead of a UUID. Re-running a case now updates the same application rather
than creating a duplicate, and the demo can script portal outcomes.

**Insurance cover was split out of "rupees recovered".** The first working demo
reported ₹10,50,900 recovered. Inspection showed ₹9,00,000 of that was PM-JAY
and the two Jan Suraksha policies — *cover amounts*, payable only on a claim.
Reporting them as cash would have been the kind of number that wins a demo and
fails an audit. `rupees_recovered_annual` is now cash only (₹1,50,900), with
`insurance_cover_secured_inr` reported separately.

**An empty resume payload became a 422.** `SubmissionDecision` defaults to
`action="submit"`, so treating `{}` as "accept the defaults" would let a
malformed POST file an application with a government department. LangGraph also
declines to resume on a falsy value, so the gate would have appeared to
re-raise itself for no reason.

**The demo family gained two documents.** With only Aadhaar, a ration card and
a passbook, every single application blocked on a missing photograph — correct
behaviour, but it hid the rest of the machine. Adding a photograph and the
children's birth certificates produces a realistic split: 8 proceed, 7 block on
certificates the family genuinely would not have.

**`build_lifecycle_graph` gained an optional checkpointer.** Normally None, so
the subgraph inherits the parent's. Passing one lets the subgraph be tested
standalone, which means a routing bug fails with a two-node trace instead of a
forty-node one.

### Final state

236 tests, 96% coverage, full suite in about eleven seconds with no network,
no API key and no database. The demo runs end to end offline and is pinned as
a regression test so the story it tells cannot drift silently.
