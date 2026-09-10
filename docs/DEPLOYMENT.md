# Deployment

One process serves the API and the console, so Yogya fits any free tier that
gives you a single web service. No database, no vector store, no worker, no
second thing to keep awake.

## Locally

```bash
uvicorn yogya.api.app:app --reload --port 8000
```

No API key needed. With none present, `get_llm()` logs a warning and runs
offline: rules, document guidance and form filling are unaffected; only the
natural-language wording falls back to templates.

## Docker

```bash
docker build -t yogya .
docker run -p 7860:7860 -e GROQ_API_KEY=... yogya
```

Defaults to port 7860 (Hugging Face's expectation) and honours `$PORT` (Render's).
Runs as a non-root user with a healthcheck on `/api/health`.

## Hugging Face Spaces (free)

1. Create a Space, SDK **Docker**.
2. Push this repository. The `Dockerfile` is picked up as-is.
3. Settings → Repository secrets → add `GROQ_API_KEY` (optional).

Spaces exposes 7860, which the image already uses. Free Spaces sleep after
inactivity.

## Render (free)

`render.yaml` is committed:

```yaml
services:
  - type: web
    name: yogya
    runtime: docker
    plan: free
    healthCheckPath: /api/health
```

Set `GROQ_API_KEY` in the dashboard (it is marked `sync: false`, so it is never
committed). Free instances sleep after 15 minutes and cold-start in about a
minute.

## What free tiers cost you

**In-flight sessions do not survive a nap.** `InMemorySaver` holds the state of
a run; when the instance sleeps, that state is gone and a half-finished
screening cannot be resumed. Completed cases persisted to the store are lost
too, since the store is in-memory as well.

For a demo this is acceptable. For a pilot it is not, and the fix is small.

## Moving to durable state

```bash
pip install langgraph-checkpoint-postgres psycopg[binary]
```

```python
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore

from yogya.api.app import build_app
from yogya.graph.build import default_serializer
from yogya.store import CaseStore

DSN = os.environ["DATABASE_URL"]

with PostgresStore.from_conn_string(DSN) as store, \
     PostgresSaver.from_conn_string(DSN) as saver:
    store.setup()
    saver.setup()
    app = build_app(
        checkpointer=saver,
        case_store=CaseStore(store),
    )
```

Nothing else changes. `build_app` and `build_graph` take both as parameters
precisely so this is a wiring change rather than a rewrite.

Note that `PostgresSaver` should be constructed with `serde=default_serializer()`
if you construct it directly — our model classes must be declared to the
serialiser or they deserialise as plain dicts.

## Configuration

Everything is `YOGYA_`-prefixed and read once, in `config.py`.

| Variable | Default | Notes |
|----------|---------|-------|
| `YOGYA_LLM_MODEL` | `groq/llama-3.3-70b-versatile` | any LiteLLM model string |
| `YOGYA_LLM_FALLBACK_MODELS` | `[]` | JSON list, tried in order |
| `YOGYA_LLM_OFFLINE` | `false` | force template-only mode |
| `YOGYA_REQUIRE_HUMAN_APPROVAL` | `true` | does **not** affect the submission gate |
| `YOGYA_MAX_APPEAL_ATTEMPTS` | `2` | before escalation |
| `YOGYA_CORPUS_DIR` | packaged | point at a different corpus |
| `YOGYA_CORS_ORIGINS` | `["*"]` | narrow this if the API is used cross-origin |
| `PORT` | `7860` | honoured by the Docker command |

Provider keys use the provider's own name — `GROQ_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY` — because LiteLLM reads them directly.

## Operational notes

- **`/api/health`** reports the corpus snapshot, scheme count, model and
  whether it is running offline. It is the healthcheck and the first thing to
  look at when something seems wrong.
- **Model cost is small by construction.** Two calls per screening plus one per
  form and per appeal letter. The eligibility sweep — the expensive-looking
  part — makes at most one wording call per scheme and none at all offline.
- **Scaling.** Sessions are keyed by thread id in the checkpointer, so multiple
  instances work as soon as the checkpointer is shared (i.e. Postgres). Nothing
  in the app holds cross-request state except the injected store.
