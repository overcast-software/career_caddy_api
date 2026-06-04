# api/CLAUDE.md

Guidance for Claude Code when working in `api/` (Django + DRF +
JSON:API + MCP). This file is a pointer; the canonical state lives
in `api/notes.org`.

## Source of truth — read FIRST

- **`api/notes.org`** (drill via `claude/cap-*`) — auth scheme,
  dedupe pipeline contract, JSON:API conventions, MCP composites vs
  CRUD split, migration gotchas.
- **Parent `todo.org`** (drill via `claude/cc-*`) — api work-items
  are filed under the parent `Inbox`; there is no `api/todo.org`.

Boot sequence (every cc-api session):

```
emacsclient --eval '(claude/cap-help)'
emacsclient --eval '(claude/cap-notes-toc)'
emacsclient --eval '(claude/cap-notes-read "Architecture/Auth scheme — Bearer not Api-Key")'
emacsclient --eval '(claude/cap-notes-read "Architecture/Dedupe pipeline contract")'
emacsclient --eval '(claude/cap-notes-read "Architecture/API basic CRUD, MCP composites")'
```

For scrape-ingestion or JobPost write-path work, also read
`Architecture/Dedupe-first on new write paths` before adding the
endpoint.

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
live in `api/notes.org Architecture/*`. The wiki is the source; this
file does not duplicate them.

## Running tests + lint

From the parent repo:

```
make test-api PATHS="<paths>"     # focused tests via pytest
make lint-api PATHS="<files>"     # ruff check in api container
make ci                           # parent Dagger gate (lint + test) before push
```

Do not run `uv run` directly on the host — Django commands run inside
the api container (`make shell-api`).
