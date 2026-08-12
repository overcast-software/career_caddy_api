# api/CLAUDE.md

Guidance for Claude Code when working in `api/` (Django + DRF +
JSON:API + MCP). This file is a quickstart; durable canon lives in
claudex.

## Source of truth — read FIRST

**claudex is the source of truth for priming.** Boot every cc-api
session from it, with an explicit `projectId` (the dockerized MCP
CWD-detects to a bogus `-app`):

```
mcp__claudex__get_project_context  projectId=-home-oldbones-Network-syncthing-Projects-career-caddy-api
mcp__claudex__recall_memory        projectId=-home-oldbones-Network-syncthing-Projects-career-caddy-api
```

Also recall the parent's `bootstrap` map memory under
`-home-oldbones-Network-syncthing-Projects-career-caddy` for the
cross-repo orientation. The api canon that used to live in
`api/notes.org Architecture/*` is now claudex memories — auth scheme
(`api-auth-scheme-bearer-not-api-key`), the dedupe pipeline contract,
JSON:API conventions, the CRUD-vs-MCP-composites split, migration
gotchas, and the fast-test recipe (`api-fast-test-recipe`).

Work state lives on the **PACA** board (Platform
`438e9c51-1c71-4cad-b597-8356b0b600ec`, prefix `CC`; Backend `BACK`),
not in an org file.

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

All conventions — auth scheme, dedupe contract, JSON:API patterns,
ScrapeProfile schema, MCP composites split, write-path dedupe rule —
live in claudex (`recall_memory` on the api projectId, plus the
parent's `bootstrap`). claudex is the source; this file does not
duplicate them.

## Running tests + lint

From the parent repo:

```
make test-api PATHS="<paths>"     # focused tests via pytest
make lint-api PATHS="<files>"     # ruff check in api container
make ci                           # parent Dagger gate (lint + test) before push
```

Do not run `uv run` directly on the host — Django commands run inside
the api container (`make shell-api`).
