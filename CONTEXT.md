# CONTEXT

Read this first. It is written for whoever picks this project up next —
a new contributor, a reviewer, or a fresh AI session with no memory of how it
got built. By the end you should be able to change something without breaking
a thing you did not know existed.

---

## 1. What the system is

A social worker sits with a poor family and types what they say. Yogya returns
every government benefit that family legally qualifies for, why, what paperwork
is missing, filled forms, tracked outcomes, and drafted appeals when a
department refuses something it should not have.

It is a LangGraph multi-agent system with seven agents, two graphs, four human
approval gates and a deterministic rule engine at its centre.

**The one-line version:** rules decide, the model explains, the human approves.

---

## 2. The three ideas everything else follows from

### 2.1 The LLM never decides eligibility

`yogya/rules/engine.py` computes verdicts from structured `Criterion` objects.
The model is handed the *finished* verdict and asked to phrase it.

Break this and you lose reproducibility, auditability, offline operation, and
the ability to defend an appeal. It is not a style preference; it is the
foundation the rest is built on. `test_matcher_never_asks_the_model_to_decide`
and `test_the_model_cannot_manufacture_a_wrongful_denial` guard it in both
directions.

### 2.2 Unknown is a third answer

Not eligible, not ineligible — *unknown*. A family who does not know their own
annual income is the normal case. `evaluate_criterion` returns three-valued
results, and a scheme with unknowns is `NEEDS_INFO`, never a guess.

An unknown that reaches a form becomes `<<NEEDS HUMAN>>`, and a form with any
flagged field cannot be submitted. This is why `form_error_rate` is a metric
rather than a hope.

### 2.3 The human gate is structural, not configurable

Four interrupts: confirm profile, select schemes, approve submission, approve
appeal. `require_human_approval=False` downgrades the first two for tests. It
does *not* touch the submission gate — see `SUBMISSION_GATE_IS_MANDATORY` in
`yogya/agents/interrupts.py`, named loudly so anyone adding a bypass has to
delete that line and explain it in a diff.

---

## 3. Where everything lives

```
src/yogya/
  config.py            all settings; nothing else reads os.environ
  audit.py             the only sanctioned way to make an AuditEntry
  portal.py            the government-portal seam (simulated, deterministic)
  store.py             long-lived case state, over LangGraph's Store
  demo.py              the runnable demo case — also a regression fixture

  models/
    household.py       Household, Member — field names mirror scheme notifications
    scheme.py          Scheme, Criterion, EligibilityResult
    application.py     Application lifecycle, Appeal, AuditEntry, RunMetrics
    state.py           the two graph state schemas — READ THE DOCSTRING

  rules/engine.py      deterministic eligibility. The heart.
  data/
    loader.py          corpus loading + version resolution
    corpus/*.json      22 schemes, structured and cited
  retrieval/index.py   BM25 candidate generation (never filters, only orders)
  ingest/myscheme.py   fetch → propose criteria → mark for human review

  llm/
    client.py          LiteLLM wrapper; provider is a config string
    fake.py            deterministic stand-in — every test uses it

  agents/              the seven agents, one file each
  graph/
    lifecycle.py       per-application subgraph (the rejection/appeal cycle)
    build.py           outer graph (map-reduce sweep + application loop)
    runner.py          driving the graph across interrupts

  api/app.py           FastAPI: API + static console in one process
web/                   the console: one HTML, one CSS, one JS. No build step.
```

---

## 4. How a run actually goes

1. `load_case` — has this family been seen before? Prior confirmed values are
   fetched so a repeat visit is "what changed", not the same twenty questions.
2. `profile` — the model turns Hinglish notes into a `Household`, declaring
   anything it could not determine.
3. **GATE:** confirm the profile. Corrections here change downstream verdicts.
4. `retrieve` — BM25 orders every scheme applicable to that state. It does not
   drop any: a missed scheme is a family who never hears about it.
5. `check_eligibility` × N — **parallel** (`Send`). Each branch runs the rule
   engine and appends to the additive `eligibility` channel.
6. `collect_results` — the reduce step.
7. **GATE:** which benefits to file, and which the family already receives.
8. `prepare_applications` — one `Application` per selection, deterministic id.
9. Loop: `next_application` → `application_lifecycle` subgraph → `archive`.
10. `summarise` → `persist` to the case store.

Inside the subgraph: documents → form → **GATE: approve submission** → submit →
poll → (approved: end) / (rejected: re-judge against the rules → wrongful? →
draft appeal → **GATE: approve appeal** → back to poll) / (attempts spent:
escalate to the grievance authority).

---

## 5. Traps that have already cost time

### 5.1 Node input is filtered by its type annotation

LangGraph inspects a node function's first-parameter annotation and passes only
those keys. Nodes typed `YogyaState` could not see `pending_applications`, so
the application queue silently read as empty and the whole lifecycle was
skipped with no error. **Every node in `build.py` is annotated
`OrchestratorState` for this reason.** If you add a state key, put it on the
schema the nodes are annotated with.

### 5.2 Never share an accumulator channel with a subgraph

The subgraph once declared `audit: Annotated[list, operator.add]` — same name
and reducer as the parent. It was seeded with the parent's whole audit list,
appended to it, and returned all of it; the parent's reducer then appended
*that* to its own copy. The log doubled per application: 63 → 283 → 1143 →
… → 587,167 entries, presenting as a graph that took a minute per application
rather than as anything visibly wrong.

The subgraph now accumulates into `run_audit` / `run_metrics`, which
`next_application` clears and `archive` folds in exactly once. See
`ApplicationState`'s docstring and `test_audit_trail_does_not_duplicate`.

### 5.3 Every model in graph state must be declared to the serialiser

`default_serializer()` in `build.py` lists our model classes. A type that is
missing does not raise — it comes back as a plain **dict**, and you find out
somewhere far away as `'dict' object has no attribute 'unknown_fields'`. Add
new state-travelling models to `YOGYA_CHECKPOINT_TYPES`.

### 5.4 Recursion limit

One application costs three super-steps. A family filing fifteen blows past
LangGraph's default of 25. Use `run_config()`, which sets 150.

### 5.5 An empty resume payload is not consent

`SubmissionDecision` defaults to `action="submit"`. LangGraph also declines to
resume on a falsy value. So `POST /resume {"decision": {}}` would either
mysteriously re-raise the gate or, if defaulted, file an application with a
government department. The API returns 422.

---

## 6. Conventions

- **Dependency injection everywhere.** `build_graph` and `build_app` take the
  llm, corpus, portal, store, settings and checkpointer. This is why the tests
  need no network, no keys and no database.
- **Agents return `(result, AuditEntry)`.** Build entries only via
  `yogya.audit.entry`.
- **Corpus edits need a citation.** Every `Criterion` carries the clause it
  encodes. A criterion without one is a claim we cannot defend.
- **Model failure is never load-bearing.** Every model call has a deterministic
  fallback. Wrap new ones the same way.
- **Rupee values are estimates**, and insurance cover is never counted as cash.

---

## 7. Where to start reading code

1. `models/household.py` and `models/scheme.py` — the vocabulary
2. `rules/engine.py` — the whole idea, in 150 lines
3. `graph/lifecycle.py` — the rejection/appeal cycle
4. `graph/build.py` — the map-reduce sweep and the application loop
5. `demo.py` — everything above, wired to a real family

## 8. Where to start reading docs

`GOALS.md` for what this is and is not trying to be. `HISTORY.md` for what has
already been tried. `docs/DECISIONS.md` for the alternatives that were
considered and rejected, so you do not re-litigate them by accident.
