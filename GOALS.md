# GOALS

What Yogya is for, what it deliberately is not, and how to tell whether it is
working.

---

## 1. The problem

Indian welfare entitlement is not primarily a rejection problem. It is a
**discovery and navigation** problem:

- Schemes are fragmented across central ministries, state departments and
  local bodies, each with its own portal, form and vocabulary.
- Eligibility is written in legal English in gazette notifications.
- The people who qualify most are least able to read them.
- Applications are refused for fixable reasons and almost nobody appeals,
  because appealing requires knowing you may.

A family can be below the poverty line for a decade, qualify for sixteen
benefits, and receive two — with no single moment where anyone said no.

---

## 2. What Yogya sets out to do

**Primary goal.** Turn one conversation with a family into a complete,
defensible, actionable list of what they are legally owed — and then do the
navigation work, with the social worker in control at every consequential step.

Four sub-goals, in priority order:

1. **Discover.** Screen every applicable scheme, not the ones someone thought
   to check. Recall beats precision: a missed scheme is a family who never
   hears about it; a spurious candidate costs one cheap rule evaluation.
2. **Explain.** Every verdict traceable to the clause that produced it, in
   language the family understands and the caseworker can defend at a counter.
3. **Unblock.** Naming a missing document is useless without saying where to
   get it, how long it takes and what it costs.
4. **Persist.** Follow the application after submission. Recognise a wrongful
   refusal, appeal it, and escalate rather than quietly giving up.

---

## 3. Non-goals

Stated plainly, so nobody builds them by accident:

- **Not an autonomous filing bot.** If the goal were throughput, the human
  gates would be the first thing to remove. They are the point.
- **Not a legal adviser.** It reports what published criteria say. It does not
  advise on disputes, interpret case law, or claim authority.
- **Not a chatbot.** The interface is a structured intake and four decision
  cards. A conversational surface would invite the model into decisions it must
  not make.
- **Not a beneficiary-facing app.** It is built for the trained worker who
  sits with the family, because that person can verify what the family says and
  is accountable for what gets filed.
- **Not a complete corpus.** 22 schemes, hand-authored, for one state plus
  central. The ingestion pipeline exists to grow that; the review gate exists
  to stop it growing carelessly.
- **Not a benefits calculator.** Rupee figures are indicative and labelled as
  estimates.

---

## 4. Design commitments

Commitments, not preferences. Changing one changes what the project is.

| # | Commitment | Consequence if abandoned |
|---|-----------|--------------------------|
| 1 | Eligibility is computed by a deterministic rule engine, never by a model | Verdicts stop being reproducible or defensible; appeals lose their grounds |
| 2 | Unknown is a distinct third state | Missing data silently becomes a default, and a family is told the wrong thing |
| 3 | The submission gate is unconditional | The system can act on a family's behalf without anyone seeing what it sent |
| 4 | Scheme rules are versioned; decisions record their version | A rejection gets re-judged against rules that did not exist when it was made |
| 5 | Every criterion carries a citation | "You qualify" becomes an assertion instead of an argument |
| 6 | Auto-ingested criteria need human review before going live | A regex over prose starts deciding who eats |
| 7 | Model failure is never load-bearing | The system stops working exactly when the free tier runs out of quota |
| 8 | Runs with no API key, no database, no vector store | Cost becomes a barrier to the thing being used at all |

---

## 5. How success is measured

The metrics the system computes rather than asserts (`RunMetrics`,
`summarise`):

| Metric | What it tells you | Watch for |
|--------|-------------------|-----------|
| `benefits_discovered` | Discovery working | Compare against what the family already receives — the gap *is* the product |
| `already_receiving` | Not double-filing | — |
| `applications_filed` | Conversion from discovery to action | — |
| `blocked_on_documents` | Where the real friction is | High is not a failure; it is the finding. Paperwork, not eligibility, is the bottleneck |
| `appeals_filed` / `appeal_success_rate` | Whether persistence pays | A rate near zero means the wrongful-denial assessment is too eager |
| `wrongful_denials_recovered` | The headline claim | Must always be backed by an audit entry citing the contradiction |
| `rupees_recovered_annual` | Cash the family receives | **Cash only.** Insurance cover is reported separately — a family cannot spend a sum insured |
| `form_error_rate` | Fields needing a human | Should be low but never zero-by-guessing |
| `human_interrupts` | Oversight actually happening | If this falls while filings rise, the gates are being bypassed |

**The anti-metric.** No metric should ever be improvable by having the model
decide more. If a change raises `benefits_discovered` by loosening the rule
engine, that is a regression.

---

## 6. Current state (v1.0)

Done:

- 7 agents, 2 graphs, 4 gates, deterministic rule engine
- 22 versioned schemes with cited criteria, including one superseded rule set
- Parallel eligibility sweep; sequential application lifecycle with the
  rejection → appeal → re-poll cycle and bounded escalation
- Long-lived case state across sessions
- myScheme ingestion with a human review gate
- FastAPI + a no-build console, single-service free-tier deployable
- 236 tests, 96% coverage, no network required

Known gaps, honestly:

- Portal submission is simulated
- Corpus covers one state plus central schemes
- Criteria simplify notifications that have exceptions and amendments
- In-memory checkpointing — a free-tier nap loses an in-flight session
- The console is English-only; the *intake* accepts Hindi but the UI does not
  speak it

---

## 7. What would come next, in order

1. **Hindi UI.** The intake understands Hindi; the screen does not. That is
   backwards for the actual user.
2. **Postgres checkpointing and store.** Two-line change, documented in
   `docs/DEPLOYMENT.md`. Needed before any real pilot.
3. **More states.** The corpus format supports it; each state needs a reviewed
   ingest pass.
4. **Document OCR at intake.** A photographed ration card answers three
   critical fields at once — the highest-leverage remaining feature.
5. **Real portal integration**, one scheme at a time, behind the gate that
   already exists.
6. **Offline-first field client.** Rural connectivity is the real constraint,
   and nothing about the architecture prevents it.

Deliberately *not* next: a conversational interface, autonomous filing, or
anything that moves a decision from the rule engine to the model.
