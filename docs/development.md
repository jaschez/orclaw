# Development

> Local development guide for `orclaw`. Server deployment is in `deployment.md`.

## Prerequisites

- Python 3.11+
- `gh` CLI authenticated (for any integration test that hits GitHub)
- SQLite 3 (system-provided on macOS/Linux; bundled with Python on Windows)

## Setup

```bash
git clone https://github.com/jaschez/orclaw.git
cd orclaw

python3.11 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

The editable install registers the `orclaw` CLI in your virtualenv.

## Running the CLI locally

The CLI works out of the box without a server. It looks for config in
`./config/` (in this repo) and a SQLite DB at `/var/lib/orclaw/data/engine.db`
by default — override the data dir with `ORCLAW_CONFIG_DIR` or a
custom `PathsSettings` (sprint 2+ adds a `--data-dir` flag).

```bash
# Show version + git rev
orclaw version

# Print resolved settings (no secrets shown)
orclaw config show

# Create the DB locally for development
mkdir -p ./dev-data
ORCLAW_DATA_DIR=./dev-data orclaw db init   # sprint 2 wires this env var

# Show status (empty until orchestrator writes batches/runs)
orclaw status
```

> Sprint-1 caveat: the CLI uses the production paths in `PathsSettings`
> unless you override `ORCLAW_CONFIG_DIR` to a dir containing a custom
> TOML that redirects them. Sprint 2 makes this cleaner with a global
> `--data-dir`.

## Running tests

```bash
pytest                          # unit tests
pytest -m "not slow"            # skip slow tests
pytest --cov=orclaw       # with coverage
```

We target 80%+ coverage on the foundational layers (config, db, parser).
Edge function specifics are covered by tests that arrive in later sprints.

## Linting and formatting

```bash
ruff check .                    # lint
ruff format .                   # format (idempotent)
mypy orclaw               # type-check
```

CI runs all three in `.github/workflows/ci.yml` (to be added when the
repo grows).

## Project layout

```
orclaw/
├── __init__.py             # version
├── __main__.py             # python -m orclaw
├── cli.py                  # Click entry point
├── config.py               # TOML + env loader, Settings dataclass
├── db.py                   # SQLite connection + init_db
├── dependency_parser.py    # parse "Blocked by #N"
├── exceptions.py           # OrclawError hierarchy
├── github_client.py        # async REST/GraphQL client
├── logging.py              # structlog setup
└── models.py               # frozen dataclasses: Issue, PR, Run, Batch, ...

tests/
├── conftest.py             # shared fixtures
├── test_config.py
├── test_db.py
├── test_dependency_parser.py
└── test_github_client.py
```

Sprints 2-5 add:

- `orclaw/orchestrator/` — long-running coordinator + loop
- `orclaw/batch_planner/` — dep-graph layering
- `orclaw/agents/` — wrappers that build and post @claude comments
- `orclaw/notifications/` — Telegram, Slack, Healthchecks
- `orclaw/dashboard/` — FastAPI HTTP server

## Style

- Type annotations on every new function (including private). `mypy --strict`.
- Public symbols documented with one-line docstrings minimum. Modules get
  a top-of-file docstring explaining *why* the file exists.
- Errors raised through the `OrclawError` hierarchy — never bare
  `Exception` or `RuntimeError`.
- Logging: `structlog` only. Never use `print` outside the CLI.

## Running against the live PAT

For end-to-end tests against the real `${TARGET_REPO}` repo, set:

```bash
export GITHUB_TOKEN=ghp_...
export GITHUB_REPO=${TARGET_REPO}
```

…then run scripts under `scripts/dev/` (added in sprint 2). Be sure your
PAT has the right scopes (`repo`, `project`, `workflow`).

## Common mistakes when adding modules

- Forgetting `from __future__ import annotations` at the top — needed for
  `str | None` syntax to type-check on 3.11.
- Re-implementing logger setup inside a module — call `get_logger(__name__)`
  and rely on the global config from `configure_logging()`.
- Catching `Exception` broadly — catch the domain-specific exception from
  `orclaw.exceptions`.
