# api/CLAUDE.md

Guidance for working in `api/` (Django + DRF + JSON:API). New to the
project? Start with the repo-root [CONTRIBUTING.md](../CONTRIBUTING.md).

## The rules that bite

These are the ones that have actually cost someone time. Read them before
writing code here.

### Auth is `Bearer`, never `Api-Key`

Two credential shapes, **one header**:

- Frontend → api: a JWT, `Authorization: Bearer <jwt>`, 60-min lifetime,
  auto-refreshed by the Ember adapter.
- Agents / automation / the browser extension → api: a long-lived API key with
  a `jh_` prefix, on the **same** header: `Authorization: Bearer <jh_...>`.
  Managed via `/api/v1/api-keys/`.

There is **no `Api-Key` wire scheme.** Sending one returns a 401 whose body
reads like an empty result set — people have chased that for an hour. If a
request mysteriously returns nothing, check the HTTP status before the query.

### Dedupe first on every JobPost write path

The same role posted to LinkedIn, Greenhouse and Lever must collapse into one
canonical record. Any new way to create a JobPost goes through
`job_hunting/models/job_post_dedupe.py` — no exceptions, including endpoints
that feel like a special case. Link normalization is subtle (see the
`canonical_link` handling); over-eager matching merges distinct jobs, which is
worse than a duplicate.

### JSON:API, snake_case

`Content-Type: application/vnd.api+json`. Attribute keys are **snake_case** —
this codebase deliberately does *not* dasherize, unlike the JSON:API default.
The router accepts both `/endpoint` and `/endpoint/`.

### New tables are a code smell

Do not add a Django model + migration without discussing the schema question
on its own. Default to reusing what exists: an extra nullable column, a
status/lifecycle value, a JSONB blob, or a verb endpoint with no persistence
at all. `ScrapeProfile` uses JSONB by design precisely so per-domain tuning
needs no migration. Signals you're about to violate this: `makemigrations`
producing a `CreateModel`, a new file in `models/`, a new pk sequence.

### CRUD stays thin; composites live in the MCP layer

Viewsets do straightforward CRUD. Multi-lookup or composite operations
(find-then-create, cross-model orchestration) belong in the MCP tool layer,
not in a bespoke endpoint.

### Model selection for AI features

Every AI role resolves its model through the registry in
`job_hunting/api/views/admin.py` `_agent_role_specs()`, surfaced by the
staff-only `GET /api/v1/agent-models/` (`urls.py`; **not** under `/admin/`).

Precedence is `<ROLE>_MODEL` → `CADDY_DEFAULT_MODEL` → a per-role default —
**but only for the rows that opt in.** `CADDY_DEFAULT_MODEL` reaches a role
only where `_agent_role_specs()` passes `global_default` for it: `caddy`,
`chat`, `job_extractor`, `browser_scraper`, and `job_parser` (conditionally).
Every role carrying a *literal* default — `chat_smart`, `hint_generator`,
`description_arbiter`, `answer`, `cover_letter`, `job_matcher` — ignores it
entirely, and so does the runtime code behind them: `JobMatcher.__init__`
reads `JOB_MATCHER_MODEL` and its own `_DEFAULT_MODEL`, nothing else. Setting
`CADDY_DEFAULT_MODEL` and expecting it to move the whole fleet will silently
miss the roles you most care about.

**If you add an AI-backed feature, register its role.** Three roles have now
been hardcoded to a model with no registry entry — `answer` and `cover_letter`
(the two that generate prose a user sends an employer) and `job_matcher`,
which decides *which company's* context those answers are written against.
None of them were visible on the admin page, so nobody could see what they
ran on. `tests/test_agent_model_registry.py` is what makes dropping a
registration fail loudly now.

Note there are two model plumbing families and they are not interchangeable:

- pydantic-ai roles take **provider-prefixed** ids (`openai:gpt-5`,
  `anthropic:claude-sonnet-4-6`) and dispatch to the right SDK — see
  `lib/parsers/job_post_extractor.py::_build_agent_for_model`.
- `AnswerService` / `CoverLetterService` call the **raw OpenAI SDK** via
  `lib/ai_client.get_client()`, which is OpenAI-only and needs a **bare**
  model id. `lib/ai_client.resolve_model()` bridges them and **raises** on a
  non-openai prefix rather than silently handing a Claude model name to the
  OpenAI client.

Some newer OpenAI models (the gpt-5 line, o-series) **reject an explicit
`temperature`** with a 400 and accept only the default. Both prose services
retry once without it and remember the rejection. Verified against the live
API; the detection matches the error *text*, not a model list, because the
message is stable and the model set isn't.

## Testing

From the repo root:

```
make test-api PATHS="job_hunting.tests.test_your_module"   # focused
make test-api                                              # full suite (~1700, slow)
make lint-api PATHS="<files>"                              # ruff
make ci                                                    # the real gate
```

Read the output, not the exit code — grep for `Ran N tests` and `OK`. The
Dagger gate asserts exactly that internally, plus `ruff` and `bandit -ll`
(stricter than the local lint), so a green `make ci` is meaningful.

Do **not** grind the full suite while iterating. It's slow, and running it
concurrently with another full run drops the shared test database mid-run and
fabricates failures that look like real ones.

Django commands run **inside the container** (`make shell-api`); don't
`uv run` on the host.

For scrape-ingestion or JobPost write-path work, recall the
dedupe-first convention before adding the endpoint — it is a hard rule
on every JobPost write path.

### RETIRED for agents — do not use

`api/notes.org` and the parent `todo.org` are Doug's personal emacs
surface: no `Read`, no writes, no commits. The `claude/cap-*` /
`cc-todo-*` emacsclient helpers no longer exist — `~/.config/doom/elisp/`
was deleted 2026-08-04, so calling one returns a void-function error.
Do not reintroduce them into a boot sequence.

## What this submodule is

Django + DRF backend serving JSON:API on `:8000` (local) / `:8025`
(prod). Hosts the MCP servers under `agents/mcp_servers/` only at the
runtime layer — code for them lives in `agents/`, not here. Auth is
JWT for the frontend, long-lived `jh_*` API keys (Bearer header) for
agents and automation.

## Stack

- Python 3.13+, Django (current LTS), DRF, drf-json-api
- PostgreSQL via Docker (`db` service in parent compose)
- SQLAlchemy on the side for some dedupe queries (legacy)
- `uv` for dependency management
- `pytest` for tests; `ruff` for lint

## Conventions

Written up under **"The rules that bite"** at the top of this file — auth
scheme, the dedupe contract, JSON:API shape, the no-new-tables rule, the
CRUD-vs-MCP split, and model selection for AI features.

Maintainers: claudex carries the incident history and fleet state behind these
(`recall_memory` on the api projectId, plus the parent's `bootstrap`). The
*rules* live here in the repo, on purpose — a convention that exists only in a
private memory service is invisible to every contributor and silently rots.
When you learn a durable rule, write it here first.
