# Architecture decision records

Each records the alternatives that were rejected, so they are not
re-litigated by accident.

---

## ADR-001 — The rule engine decides eligibility; the LLM only explains

**Status:** accepted, foundational.

**Context.** The obvious design is to retrieve scheme documents and ask a model
whether a household qualifies. It is fast to build and demos well.

**Decision.** Eligibility is computed by pure functions over structured
`Criterion` objects. The model receives the finished verdict and phrases it.

**Rejected: LLM-as-judge over retrieved text.**
- Not reproducible — the same family can get different answers.
- Not defensible — "the model said so" fails at a block office counter.
- Not auditable — you cannot cite which clause produced the outcome.
- Not free — every screening costs tokens, on a project whose users have none.
- Fails silently in the direction that hurts: a hallucinated *no* looks exactly
  like a real *no*, and nobody appeals it.

**Consequences.** Someone must encode criteria as structured data, which is
real work and is why the corpus is 22 schemes rather than 2,000. In exchange:
offline operation, reproducibility, citations, and appeals with actual grounds.

---

## ADR-002 — Three-valued eligibility logic

**Status:** accepted.

**Context.** A family that does not know its own annual income is the normal
case, not an edge case.

**Decision.** `CriterionResult` carries `passed` *and* `unknown`. A scheme with
unknowns and no hard failure is `NEEDS_INFO`. Each criterion declares whether
an unknown blocks or merely warns.

**Rejected: default unknown to a value.** Zero makes everyone eligible for
everything; infinity makes them eligible for nothing. Both are confidently
wrong, in opposite directions.

**Rejected: refuse to evaluate until complete.** Most families would never get
past intake, and the value of the system is precisely that it works from a
partial picture.

---

## ADR-003 — BM25 retrieval, exhaustive rather than top-k

**Status:** accepted.

**Decision.** `rank-bm25` over normalised scheme text, plus hard structural
filtering by state. `include_all_applicable=True` by default: retrieval
*orders* candidates and never drops them.

**Rejected: a vector database.** The corpus is a few hundred documents, not a
few million. BM25 plus structural filters beats embeddings at this scale, needs
no vector service on a free tier, costs no embedding API calls, and — the
deciding factor — is inspectable. You can explain to a caseworker why a scheme
surfaced.

**Rejected: top-k truncation.** A missed scheme is a family who never hears
about it. A spurious candidate costs one deterministic rule evaluation, roughly
a microsecond. The asymmetry is enormous, so recall wins outright. Retrieval is
a *candidate generator*; the rule engine does the rejecting.

**Revisit when** the corpus exceeds a few thousand schemes, at which point
exhaustive evaluation stops being free and top-k with a generous k, or a hybrid
retriever, becomes worth the complexity.

---

## ADR-004 — Provider-agnostic LLM access via LiteLLM

**Status:** accepted.

**Decision.** One model string in config. Groq, Gemini, OpenAI, Ollama and
anything else LiteLLM supports work without a code change. Fallback models are
tried in order.

**Context.** Free tiers run out of quota mid-afternoon and change terms without
warning. Being able to move providers by editing an environment variable is
worth the thin abstraction layer. `get_llm()` degrades to the deterministic
fake when no key is present, so a deployment with no key is a working
deployment rather than a broken one.

---

## ADR-005 — Parallel eligibility, sequential applications

**Status:** accepted; changed during the build.

**Context.** The original plan fanned out application lifecycles with `Send`,
symmetrically with the eligibility sweep.

**Decision.** Eligibility fans out with `Send`. Applications run through the
subgraph one at a time, via a queue in state and a loop edge.

**Why.** Eligibility checks are pure, fast and interrupt-free, so parallelism
is free and real. Application lifecycles contain human interrupts; N in
parallel raise N simultaneous interrupts and force the caller to resume them by
interrupt id. More importantly, a caseworker approves one form at a time — so
parallel gates are worse for the only user who matters.

Parallelism belongs where it helps, and should be absent where it would only
look impressive on an architecture diagram.

---

## ADR-006 — The subgraph is a real subgraph

**Status:** accepted.

**Decision.** The lifecycle is compiled once and added as a node, sharing state
keys with the parent.

**Rejected: invoking it inside a Python `for` loop in a node.** Nodes re-execute
on resume, so a loop that had completed three applications would redo all three
after the fourth interrupt. Compiling it as a node keeps checkpointing and
interrupt-resume correct across the loop.

**The trap this created:** shared *accumulator* channels double on every pass.
See ADR-007.

---

## ADR-007 — Subgraph accumulators are private, drained by the parent

**Status:** accepted, after a bug.

**Context.** The subgraph declared `audit: Annotated[list, operator.add]` — the
same key and reducer as the parent. It was seeded with the parent's whole list,
appended to it, returned all of it, and the parent's reducer appended *that* to
its own copy. The trail doubled per application: 63 → 283 → 1143 → … →
587,167 entries. It presented as an exponential slowdown, not as wrong output.

**Decision.** The subgraph accumulates into `run_audit` / `run_metrics`.
`next_application` clears them; `archive` folds them in exactly once.

**Rule to carry forward:** never share an accumulator channel between a parent
graph and a subgraph. Share plain values freely; share reducers never.

---

## ADR-008 — Ingested criteria require human review before going live

**Status:** accepted.

**Decision.** `ingest_myscheme.py` writes `*.pending.json`. The corpus loader
refuses to read that suffix. `review_ingest.py` shows the source prose beside
the proposed criteria and promotes only what a human accepts.

**Context.** Turning "applicant should belong to a BPL family and should not be
an income tax payer" into two machine-checkable criteria is a translation that
changes who gets money. Regexes are good enough to *propose* and nowhere near
good enough to *decide*. This is the same principle as ADR-001, applied to the
rules themselves rather than to their evaluation.

---

## ADR-009 — Insurance cover is not counted as cash

**Status:** accepted, after an embarrassing number.

**Context.** The first working demo reported ₹10,50,900 recovered. ₹9,00,000
of it was PM-JAY and two Jan Suraksha policies — *cover amounts*, payable only
on a claim.

**Decision.** `rupees_recovered_annual` counts only schemes whose
`benefit_kind` is not `INSURANCE`. Cover is reported separately as
`insurance_cover_secured_inr`.

**Why it is an ADR and not a bug fix.** It is a statement about what the
project is willing to claim. A family cannot spend a sum insured, and a metric
that wins a demo and fails an audit is worse than no metric.

---

## ADR-010 — In-memory state, with a documented path off it

**Status:** accepted for v1.

**Decision.** `InMemorySaver` and `InMemoryStore` by default, both injectable.

**Context.** Free tiers sleep when idle and lose memory when they do, so an
in-flight screening does not survive a nap. For a demo that is an acceptable
trade against requiring a database. The interfaces are LangGraph's, so
`PostgresSaver` and `PostgresStore` are a wiring change — see
`docs/DEPLOYMENT.md`. Needed before any real pilot; not needed to evaluate the
design.
