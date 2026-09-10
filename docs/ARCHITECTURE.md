# Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│  web/           one HTML, one CSS, one JS. No build step.    │
├──────────────────────────────────────────────────────────────┤
│  api/app.py     FastAPI — serves the API and the console      │
│                 from a single process                         │
├──────────────────────────────────────────────────────────────┤
│  graph/         build.py   outer graph, map-reduce + loop     │
│                 lifecycle.py  per-application subgraph        │
│                 runner.py  driving the graph across gates     │
├──────────────────────────────────────────────────────────────┤
│  agents/        seven agents; each returns (result, audit)    │
├──────────────────────────────────────────────────────────────┤
│  rules/engine.py      deterministic eligibility  ← the centre │
│  retrieval/index.py   BM25 candidate generation               │
│  data/loader.py       corpus + rule versioning                │
│  llm/                 LiteLLM client + deterministic fake     │
│  portal.py            government-portal seam (simulated)      │
│  store.py             long-lived case state                   │
├──────────────────────────────────────────────────────────────┤
│  models/        Household · Scheme · Application · state      │
└──────────────────────────────────────────────────────────────┘
```

Dependencies point downward only. `rules/engine.py` imports nothing but models
— it is a pure function library, which is what makes it trivially testable and
impossible to accidentally couple to a model call.

## Data flow

```
intake notes
   │  HouseholdProfiler (LLM)
   ▼
Household ──────────────┐
   │  SchemeRetriever    │  (BM25 ordering, no filtering)
   ▼                     │
candidate scheme ids     │
   │  ┌──────────────────┘
   │  │  rules/engine.py — evaluate_scheme(household, scheme)
   ▼  ▼
EligibilityResult[]  ── each carries verdict + per-criterion results + citations
   │
   │  human selects
   ▼
Application[]
   │  DocumentGapAgent → gaps
   │  FormFillerAgent  → form_data + flags
   │  human approves
   │  ApplicationTrackerAgent → portal
   ▼
decision ── approved ──► done
        └─ rejected ──► GrievanceEscalationAgent
                          │ re-runs the rule engine at the filed rule version
                          ├─ consistent  ──► stands
                          └─ contradicted ──► appeal (human approves) ──► re-poll
```

## The two state schemas

Defined in `models/state.py`. Read its docstrings before changing either.

### `OrchestratorState` (outer graph)

| Key | Reducer | Notes |
|-----|---------|-------|
| `household_id`, `intake_notes` | — | inputs |
| `household` | — | last write wins |
| `candidate_scheme_ids` | — | |
| `eligibility` | `operator.add` | **additive** — the map step's parallel branches each append |
| `selected_scheme_ids`, `already_receiving_scheme_ids` | — | from the human |
| `pending_applications` | — | the queue |
| `current_application`, `application` | — | `application` is the key the subgraph reads |
| `applications` | `merge_applications` | keyed by id, right wins |
| `audit` | `operator.add` | run-wide trail |
| `metrics` | `merge_metrics` | field-wise sum |
| `run_audit`, `run_metrics` | — | **drain channels** for the subgraph; overwritten, never accumulated at this level |
| `summary`, `corpus_snapshot` | — | outputs |

### `ApplicationState` (subgraph)

| Key | Reducer | Notes |
|-----|---------|-------|
| `household`, `application` | — | shared with the parent by name |
| `portal_outcome` | — | scratch: the submission decision, the draft appeal |
| `run_audit` | `operator.add` | private accumulator |
| `run_metrics` | `merge_metrics` | private accumulator |

**Two absences are deliberate.** The subgraph does not declare `audit` or
`metrics` — sharing an accumulator with the parent doubles it on every pass
(see `HISTORY.md`). It does not declare `eligibility` either, because in the
parent that key holds the whole sweep, and reusing the name would shadow it.

## Node input filtering

LangGraph passes a node only the keys declared on its annotated state schema.
Every node in `build.py` is annotated `OrchestratorState` for this reason;
annotating one `YogyaState` makes the queue keys invisible to it and the
failure is silent.

## Checkpointing and persistence

Two separate concerns, deliberately:

- **Checkpointer** — the state of one in-flight run, so an interrupt can be
  resumed. `InMemorySaver` by default.
- **Store** (`store.py`) — the *case*: profile, applications, audit, across
  months and across caseworkers. Restarting the process may lose a run; it must
  never lose a case.

`default_serializer()` declares our model classes to the checkpoint serialiser.
An undeclared model does not raise — it deserialises as a plain dict.

## Injection points

`build_graph(llm=, corpus=, portal=, case_store=, settings=, checkpointer=,
as_of=)` and `build_app(...)` take every dependency. Nothing constructs a
provider client, opens a socket or reads a file at import time except the
module-level `app` in `api/app.py`.

`as_of` deserves a mention: passing a past date makes the whole graph evaluate
against the rule versions in force on that date, which is what makes historical
re-assessment possible.
