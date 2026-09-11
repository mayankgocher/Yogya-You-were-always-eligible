# Yogya — *"You were always eligible."*

A family lives below the poverty line for twelve years. They qualify for
sixteen benefits. They receive two. Not because they were rejected — because
nobody ever sat down and enumerated it.

Yogya is a benefits committee in a box. A social worker types what a family
told them, in Hindi or English or both, and gets back every scheme that family
legally qualifies for, what paperwork is missing and where to get it, filled
application forms, tracked outcomes, and drafted appeals when a department
refuses something it should not have.

**The social worker is always the boss.** Nothing is submitted to a government
portal without a human reading the form and pressing submit.

```
$ python -m yogya.demo

Household : Sunita Devi (hh-sunita-devi)

-- Eligibility sweep ---------------------------------------------------
  22 schemes screened · 16 eligible · 6 not eligible

-- Applications --------------------------------------------------------
  approved               MGNREGS
  escalated              Ujjwala Yojana (appeals: 2)
  blocked_on_documents   Pre-Matric Scholarship — needs caste, income, school certificates
  approved               Old Age Pension (appeals: 1)  ← wrongly denied, recovered
  ...

-- Metrics -------------------------------------------------------------
  benefits_discovered          16
  applications_filed           15
  approved                      7
  blocked_on_documents          7
  appeals_filed                 3
  wrongful_denials_recovered    1
  rupees_recovered_annual   150900
  insurance_cover_secured  900000
  form_error_rate             0.0
  human_interrupts             13
```

---

## The one design decision that matters

**The LLM never decides eligibility.**

```
 rule engine  ──►  verdict     deterministic, versioned, citable
 LLM          ──►  wording     explanations, form declarations, appeal letters
```

A language model reading a scheme notification and deciding whether a widow
qualifies for a pension is a system that will eventually tell a family they do
not qualify when they do — and will be unable to say why. So eligibility is
computed by a pure function over structured criteria extracted from the scheme
document, each carrying the clause it came from. The model is handed the
finished verdict and asked only to phrase it.

The consequences are the good part:

- **It works with no API key at all.** Rules, document guidance and form
  filling are unaffected offline; only the prose falls back to templates.
- **Every verdict is reproducible**, byte for byte, given the same household
  and rule version.
- **"You were wrongly denied" is defensible.** When a department refuses an
  application, Yogya re-runs the same rule engine against the rule version in
  force *when the application was filed*. If the engine says eligible and the
  department says no, that contradiction — clause by clause — is the ground of
  appeal. Not a model's opinion.

---

## Quick start

```bash
git clone <your-repo> && cd Yogya

python -m venv .venv                       # Python 3.12.7 recommended
.venv\Scripts\activate                     # Windows
# source .venv/bin/activate                # macOS / Linux

pip install -r requirements-dev.txt
pip install -e . --no-deps                 # puts `yogya` (src/ layout) on the path

python -m pytest                           # 236 tests, ~11 seconds, no network
python -m yogya.demo                       # the full case, end to end
python -m yogya.demo --audit               # …with the complete audit trail

uvicorn yogya.api.app:app --reload --port 8000
# open http://localhost:8000
```

No API key is needed for any of the above. To use a real model, copy
`.env.example` to `.env` and set `GROQ_API_KEY` (or Gemini, or point
`YOGYA_LLM_MODEL` at a local Ollama).

---

## The seven agents

| # | Agent | Input → Output | Model used for |
|---|-------|----------------|----------------|
| 1 | **Household Profiler** | Hinglish intake notes → structured `Household` | extraction (the one genuinely linguistic job) |
| 2 | **Entitlement Matcher** | household + scheme → `EligibilityResult` | wording only — the rule engine decides |
| 3 | **Document Gap** | household + scheme → missing papers + where to get them | nothing; guidance is curated |
| 4 | **Form Filler** | household + scheme → form fields + flags | the free-text declaration only |
| 5 | **Application Tracker** | application → submitted / polled | nothing |
| 6 | **Grievance Escalation** | rejection → wrongful? → appeal → escalation | drafts the letter; the engine judges the rejection |
| 7 | **Social Worker Interrupt** | every consequential act → a human decision | nothing |

Agent 3 is deliberately model-free. "Where do I get an income certificate in
Uttar Pradesh" has one right answer, and a hallucinated office name costs a
family a day's wages and a bus fare.

---

## The graph

```
load_case ─► profile ─► confirm_profile ─► retrieve
                          (interrupt)          │
                                ┌──────────────┴───────────────┐
                                │   Send — one per scheme      │  ← map
                                ▼                              ▼
                         check_eligibility            check_eligibility
                                └──────────────┬───────────────┘
                                               ▼                  ← reduce
                                        collect_results
                                               │
                                       select_schemes (interrupt)
                                               │
                                     prepare_applications
                                               │
                     ┌──► next_application ──empty──► summarise ─► persist ─► END
                     │            │
                     │   application_lifecycle  (subgraph)
                     │            │
                     └──────── archive
```

and inside the subgraph, the part that makes this a graph rather than a
pipeline:

```
check_documents ──blocked──────────────────────────────► END
      │
   fill_form ──► approve_submission ──hold/skip───────► END
                     (interrupt) │ submit
                     submit ──► poll ──approved───────► END
                                  │ rejected
                          assess_rejection ──defensible► END
                                  │ wrongful
                      ┌───────────┴────────────┐
                attempts left            attempts spent
                      │                         │
                draft_appeal                escalate ──► END
                      │
              approve_appeal ──abandon───────────────► END
                 (interrupt) │ file
                 file_appeal ──────► poll   ← the cycle
```

**Why eligibility fans out and applications do not.** Eligibility checks are
pure, fast and interrupt-free, so `Send` gives real parallelism for free.
Application lifecycles contain human interrupts; running twelve in parallel
would raise twelve simultaneous interrupts and force the caller to resume them
by id. That is worse for the only user who matters — a caseworker approves one
form at a time — so applications run through the subgraph sequentially via a
queue and a loop edge. The parallelism is where it helps and absent where it
would only look impressive.

---

## Scheme data

The corpus ships with 22 real central and Uttar Pradesh schemes — PM-KISAN,
PM-JAY, the NSAP pensions, PMAY-G, Ujjwala, NFSA, MGNREGS, scholarships, the
UP state pensions and Kanya Sumangala — each with structured, cited criteria.

Rules are **versioned**, because they change. The UP old age pension is in the
corpus twice: the pre-2024 rules (income ceiling ₹46,080) and the current ones
(₹56,460). A decision records which version produced it, and an appeal is
judged against the version in force at filing time.

`scripts/ingest_myscheme.py` pulls a fresh snapshot from **myscheme.gov.in**,
the government's own aggregator. Two rules govern it:

1. **Nothing at runtime ever calls myScheme.** Ingestion writes a snapshot;
   a portal outage can never change a verdict mid-case.
2. **Auto-extracted criteria are not live.** The pipeline *proposes* criteria
   from eligibility prose, marks every scheme `needs_review`, and writes to
   `*.pending.json` — which the corpus loader refuses to read. A human promotes
   them with `scripts/review_ingest.py`. A regex deciding who eats is exactly
   the failure this project exists to prevent.

See [`docs/SCHEME_DATA.md`](docs/SCHEME_DATA.md).

---

## Deploying free

One process serves the API and the console, so it fits any free tier that
gives you a single web service.

```bash
docker build -t yogya . && docker run -p 7860:7860 yogya
```

- **Hugging Face Spaces** — Docker SDK, port 7860, works as-is
- **Render** — `render.yaml` is committed; free plan, no database

Both sleep when idle and lose in-memory sessions when they do.
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) has the two-line change to
Postgres-backed checkpointing for when that stops being acceptable.

---

## Tests

```bash
python -m pytest                              # 236 tests, ~11s
python -m pytest --cov=yogya                  # 96% coverage
python -m pytest -m "not integration"         # units only
```

No network, no API keys, no clock dependence — `pytest` passes in aeroplane
mode. The suite is built around what the system must *never* do:

- the model cannot override a verdict, in either direction
- nothing reaches a portal without passing the submission gate
- an unknown field is never filled with a guess
- the appeal cycle terminates and escalates rather than looping
- a rejection is re-judged against the rules in force when it was filed

[`docs/TESTING.md`](docs/TESTING.md) explains the invariants and why each one
has a test.

---

## Documentation

| File | What it is for |
|------|----------------|
| [`CONTEXT.md`](CONTEXT.md) | Orientation for a new contributor or a new session — the whole system in one read |
| [`GOALS.md`](GOALS.md) | What this is trying to achieve, what is deliberately out of scope, how success is measured |
| [`HISTORY.md`](HISTORY.md) | What was built when, what broke, and what the fix taught us |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module map, state schemas, data flow |
| [`docs/AGENTS.md`](docs/AGENTS.md) | Each agent's contract and its prompts |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | The models, and why the fields are named as they are |
| [`docs/SCHEME_DATA.md`](docs/SCHEME_DATA.md) | Corpus format, versioning, ingestion and review |
| [`docs/TESTING.md`](docs/TESTING.md) | Test strategy and the invariants under test |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Free-tier deployment and the path off it |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Architecture decision records, with the alternatives rejected |

---

## Honest limitations

- **The portal is simulated.** No real submissions. `yogya/portal.py` is the
  seam where real integration would go, and it stays behind the human gate.
- **The corpus is 22 schemes, hand-authored.** India has thousands. The
  ingestion pipeline is real and tested, but it needs network access this
  project was built without, and every scheme it fetches needs human review.
- **Criteria are simplifications.** Real notifications have exceptions and
  state amendments no `Criterion` list fully captures. Every value in the
  corpus is marked as needing verification against the current notification
  before real-world use.
- **Rupee values are estimates**, labelled as such. Insurance *cover* is
  reported separately from cash, because a family cannot spend a sum insured.
- **Prior art:** [Haqdarshak](https://www.haqdarshak.com/) does agent-assisted
  entitlement discovery at real scale. Yogya is not a competitor — it is an
  exploration of what the orchestration layer looks like when the human stays
  in the loop by construction.

## Licence

MIT.
