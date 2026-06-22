# Render Workflow Agents — Python

An example repo that deploys **one agentic code-review use case** across
**three Render execution substrates**: an in-process web service, a web service
plus queue-backed worker, and Render Workflows.

The same multi-agent PR reviewer (`security`, `performance`, `ux`, then a
`judge`) runs across progressively more durable execution models. Along the way,
you can inspect logs and traces in the Dashboard and use local development for
focused test loops.

The core idea: the agent does not change. The substrate does.

This is the Python sibling of [workflow-agents-workshop-ts](../workflow-agents-workshop-ts).

## The three patterns

| Pattern | Package | Substrate | Render primitives | You own |
| --- | --- | --- | --- | --- |
| **1. Naive** | [`packages/naive_agent`](packages/naive_agent) | Agent runs in-process, inside the web request | Web Service + Postgres | Nothing, but no scale or durability |
| **2. Queue** | [`packages/queue_agents`](packages/queue_agents) | Thin producer + background worker over a Valkey queue | Web Service + Background Worker + Key Value + Postgres | The queue, consumer group, acks, retries, and pub/sub |
| **3. Workflows** | [`packages/workflow_agents`](packages/workflow_agents) | Each agent is a Render `@app.task` in its own container | Web Service + Workflows + Postgres | Nothing. Render does the coordination |

The agent code lives in the shared [`workshop-agent`](shared/agent) package. The substrate decides how it is invoked.

## Start here

Before you begin, make sure you have:

- A Render account
- A fork or writable copy of this repo on GitHub, GitLab, or Bitbucket
- The Render CLI, for Workflow operations and deploy/log checks
- Python >= 3.12 and [uv](https://docs.astral.sh/uv/)

Install dependencies from the repo root:

```sh
uv sync
```

The apps run without an LLM API key. With no key set, [`workshop-agent`](shared/agent)
uses a deterministic **mock** model, so live deploys and local tests still work.
Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` for real reviews, or force the mock
with `AGENT_MODEL=mock`.

## Blueprints

Patterns 1 and 2 use Blueprints:

- [`packages/naive_agent/render.yaml`](packages/naive_agent/render.yaml) — Web Service + Postgres
- [`packages/queue_agents/render.yaml`](packages/queue_agents/render.yaml) — Web Service + Background Worker + Key Value + Postgres

Each Blueprint creates its own Render project with a `production` environment, so
the services and datastores for each pattern stay grouped in the Dashboard.

Pattern 3 uses both:

- [`packages/workflow_agents/render.yaml`](packages/workflow_agents/render.yaml) creates the web service and Postgres database
- `render workflows create` creates the Workflow service
- `render workflows start` triggers task runs
- `render logs`, `render deploys`, and the Dashboard show what ran

## Local development

Local runs are useful for tests and debugging.

For local runs, copy the example env file:

```sh
cp .env.example .env
```

Local services need only what each pattern uses:

```sh
createdb agents_workshop        # Postgres: naive-agent and queue-agents
valkey-server &                 # Valkey: queue-agents only
```

Run any pattern on the host:

```sh
# Pattern 1: in-process
python -m naive_agent.server               # http://localhost:3000

# Pattern 2: producer + worker
python -m queue_agents.web                 # terminal A: http://localhost:3000
python -m queue_agents.worker              # terminal B: one worker
python -m queue_agents.worker              # terminal C: another worker

# Pattern 3: Render Workflows (local dev mode)
RENDER_USE_LOCAL_DEV=true python -m workflow_agents.server
```

To exercise the full local Render Workflows runtime, use two terminals:

```sh
# Terminal A: local Workflow task server
cd packages/workflow_agents
render workflows dev -- uv run python -m workflow_agents.workflow
```

```sh
# Terminal B: web UI dispatching to the local Workflow task server
RENDER_USE_LOCAL_DEV=true \
RENDER_LOCAL_DEV_URL=http://127.0.0.1:8120 \
RENDER_API_KEY=local-dev \
uv run python -m workflow_agents.server
```

Open `http://localhost:3000/` for the shared telemetry viewer, paste a public PR
URL, and watch the review run with per-agent findings and spans.

## Repository structure

Start with the pattern you care about, then follow the shared core the patterns
all import.

```
packages/
  naive_agent/              Pattern 1: in-process web service (FastAPI)
                              → src/naive_agent/server.py    POST /api/reviews; pipeline blocks the HTTP response
                              → render.yaml                  single-service Blueprint

  queue_agents/             Pattern 2: producer web + background worker (Valkey)
                              → src/queue_agents/web.py      enqueue jobs, return 202
                              → src/queue_agents/worker.py   consume the queue, same pipeline as naive
                              → src/queue_agents/kv.py       Valkey stream + pub/sub wiring

  workflow_agents/          Pattern 3: Render Workflows gateway + workflow service
                              → src/workflow_agents/server.py       dispatch workflows, GitHub webhooks
                              → src/workflow_agents/workflows/code_review.py   the finished pipeline as tasks
                              → src/workflow_agents/workflows/your_review.py   starter template for custom reviews

shared/
  agent/                    workshop-agent — LLM loop, agents, composable building blocks
                              → src/workshop_agent/review.py       convenience wrapper (each pattern composes inline)
                              → src/workshop_agent/agents.py       security, performance, ux, judge definitions
                              → src/workshop_agent/loop.py         provider-agnostic LLM + tool loop

  db/                       workshop-db — telemetry store (Postgres or in-memory)
                              → src/workshop_db/__init__.py        create_review, persist_review, store_tracer
                              → src/workshop_db/memory.py          in-memory backend for local dev

  ui/                       workshop-ui — mountable FastAPI telemetry viewer
                              → src/workshop_ui/__init__.py        create_ui_router() + read APIs
                              → src/workshop_ui/templates/         dashboard HTML template

tests/                      unit, integration, and e2e tests (mock model, no API key)
                              → integration/test_workflow_app.py   core pipeline end-to-end
                              → integration/test_queue_kv.py       ack/retry contract verification
                              → conftest.py                        GitHub stub + shared fixtures
```

### Shared packages

- **[`workshop-agent`](shared/agent)** — The substrate-agnostic core. Composable
  building blocks (`prepare_diff`, `filter_diff`, `select_reviewers`,
  `to_review_summary`), the `define_agent` reviewers (`security_reviewer`,
  `performance_reviewer`, `ux_reviewer`, `judge`), the provider-agnostic LLM loop,
  and the mock client. Each pattern imports these blocks and composes the pipeline
  at its own call site so the architectural trade-offs are visible. Nothing here
  knows about Render.
- **[`workshop-db`](shared/db)** — The durable telemetry record the viewer reads.
  Auto-selects Postgres when `DATABASE_URL` is set, and uses in-memory storage
  otherwise.
- **[`workshop-ui`](shared/ui)** — A single mountable FastAPI router that renders the
  reviews table with drill-in to findings and agent spans.

## The code-review pipeline

Every pattern runs the same review:

```
prepare_diff -> filter_diff -> [ security || performance || ux? ] -> judge
```

- `prepare_diff` turns a GitHub PR URL into per-file patches. Public repos need no token.
- `filter_diff` drops noise: lock files, minified assets, source maps, and bundles.
- `security` and `performance` always run in parallel. `ux` joins when the diff
  touches frontend files (`.tsx`, `.jsx`, `.vue`, `.css`, and so on).
- `judge` consolidates findings into an approve / request-changes verdict.

Trigger one against any deployed or local web service:

```sh
curl -s -X POST "$SERVICE_URL/api/reviews" \
  -H 'content-type: application/json' \
  -d '{"prUrl":"https://github.com/octocat/Hello-World/pull/9681"}'
```

## Configuration

All patterns read the same env:

| Var | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | all | Optional. Deterministic mock model if absent |
| `AGENT_MODEL=mock` | all | Force the mock model even with a key |
| `DATABASE_URL` | naive-agent, queue-agents, workflow-agents | Postgres. In-memory fallback when unset |
| `VALKEY_URL` | queue-agents | Queue and pub/sub. Defaults to `redis://127.0.0.1:6379` |
| `PORT` | web tiers | Defaults to `3000` |
| `GITHUB_TOKEN` | all | Optional. Raises rate limits and enables private-repo diffs |
| `RENDER_USE_LOCAL_DEV` | workflow-agents | Set to `true` only for local dev |
| `RENDER_LOCAL_DEV_URL` | workflow-agents | Local Workflow task server URL |
| `RENDER_API_KEY` | workflow-agents | Required in production Workflow dispatch |
| `RENDER_WORKFLOW_SLUG` | workflow-agents | Required in production. Name or slug of the Workflow service to run |

## Testing

All suites run against the deterministic mock model, so they need no LLM provider
API key:

```sh
pytest                          # everything
pytest tests/unit               # pure logic
pytest tests/integration        # per-pattern app + worker kv contract
pytest tests/e2e                # end-to-end naive + workflow flows
```

The `test_queue_kv` integration test verifies the ack/retry contract. It requires
a local Valkey/Redis:

```sh
VALKEY_URL=redis://127.0.0.1:6379 pytest tests/integration/test_queue_kv.py -v
```

### Troubleshooting `test_queue_kv`

The queue kv test needs a running Valkey (or Redis) instance. If it hangs or
skips:

| Symptom | Cause | Fix |
| --- | --- | --- |
| All tests show "skipped" | `VALKEY_URL` not set in the environment | Run with the var inline: `VALKEY_URL=redis://127.0.0.1:6379 pytest tests/integration/test_queue_kv.py -v` |
| `ConnectionError` / `Connection refused` | No Valkey/Redis server running | Start one first: `valkey-server &` or `docker run -d -p 6379:6379 valkey/valkey` or `redis-server &` |
| Tests pass but pytest hangs | Open Redis connections preventing exit (fixed in recent commits) | Update to the latest code; if still stuck, press Ctrl-C — results are valid |

## Notes

- This is a **uv workspace** monorepo (`shared/*` and the three `packages/*`).
  Install from the root with `uv sync` — this installs all workspace members and
  their dependencies (including `redis`) into the root venv, so `uv run pytest`
  works without extra flags.
- Blueprint `buildCommand`s use `uv sync --package workshop-<pattern>` (e.g. `workshop-queue-agents`) so the
  service package and its workspace deps are installed into the Render venv.
- After changing a Blueprint, **sync the Blueprint** in the Render Dashboard so
  existing services pick up the new `buildCommand` — a manual deploy alone reuses
  the old command stored on the service.
- The mock model means the entire pipeline, all three patterns, and the full test
  suite run offline with zero credentials.
- Pattern 3 uses the Render SDK (`render_sdk`). Each module in
  `src/workflow_agents/workflows/` defines its own `Workflows` app with its
  tasks decorated in place; the loader auto-discovers the modules and
  `workflow.py` merges the apps with `Workflows.from_workflows` — no manual
  registration step.
