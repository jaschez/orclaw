# Running a local dashboard with synthetic data

Sometimes you want to play with the dashboard before pointing it at a
real target repo. Orclaw ships a `demo-seed` CLI that populates a
local SQLite with realistic-looking batches, runs, and events so every
screen has something to render.

## Use case

- Evaluating Orclaw before committing infra.
- Designing custom dashboard tweaks (extra columns, custom widgets).
- Recording screencasts / demos for talks.

## Quickstart

```bash
# 1. Fresh venv (or use the system one if you've installed via the bootstrap).
git clone https://github.com/jaschez/orclaw.git
cd orclaw
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dashboard]'

# 2. Point at a throwaway DB.
export ORCLAW_DB_PATH=$(mktemp -t orclaw-demo.XXXXXX.db)
export ORCLAW_GITHUB_REPO=example/demo
export GITHUB_TOKEN=dummy

# 3. Seed it.
orclaw demo-seed

# 4. Serve the dashboard.
orclaw dashboard serve --port 8888

# 5. Visit http://127.0.0.1:8888
```

`demo-seed` writes:

- ~12 batches across 3 layers, mix of statuses (pending / in_progress
  / merged / failed / skipped)
- ~25 runs (implementer + reviewer, success / failed / running)
- ~40 events spread across the last hour, including saturation alerts
- Plausible PR + run IDs so the UI looks "live"

Re-running `demo-seed` clobbers the previous seed in the same DB.

## Caveats

- Demo mode never posts to GitHub or Telegram — it's purely local.
- The `/api/decision_preview` endpoint **does** try to talk to GitHub
  (because it runs a real orchestrator tick). It'll fail loudly with
  `dummy` as the token — that's expected. Everything else in the
  dashboard renders fine without it.
- `orclaw doctor` will rightly complain that your config is fake.
  Ignore.
