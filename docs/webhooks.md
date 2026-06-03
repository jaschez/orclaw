# GitHub webhooks — push instead of poll

Orclaw historically learned about new issues, comments, PRs and CI
results by **polling** GitHub (`orclaw orchestrator tick` on a systemd
timer). Polling is simple and robust but laggy. The dashboard — already a
FastAPI app behind Cloudflare — now also **receives** GitHub webhook
deliveries and reacts immediately, with polling kept as a cheap
reconciliation fallback.

## How it works

```
GitHub  ──(webhook: issue_comment / pull_request / workflow_run / …)──▶  /webhook/github
                                                                            │
                                            verify X-Hub-Signature-256 (HMAC)
                                                                            │
                                                event_should_trigger? ──no──▶ 200 ignored
                                                                            │ yes
                                                       CoalescingTickRunner ─▶ apply tick
```

- **Endpoint:** `POST /webhook/github` on the dashboard service.
- **Auth:** the HMAC signature (`X-Hub-Signature-256`), **not** Cloudflare
  Access. GitHub can't present an Access cookie, so this path must be
  *excluded* from Access (see below). An unsigned or mis-signed delivery
  is rejected with `401`.
- **Coalescing:** a burst of deliveries (open PR → synchronize → CI run →
  review, all within a second) collapses into "run a tick now, then run
  exactly one more if anything arrived while busy" — never a stampede.
- **Which events trigger a tick:** `issue_comment`,
  `pull_request_review_comment`, `pull_request`, `pull_request_review`,
  `workflow_run` (completed), `issues`, `push`. Irrelevant actions (e.g.
  a PR `assigned` event) return `200 ignored` and cost nothing. `ping`
  returns `pong`.

The single-flight concurrency cap still applies: a webhook can only ever
cause **one** Claude task to be in flight at a time (see
`docs/pro-plan-strategy.md`). Webhooks make the engine *react faster*,
not *run more in parallel*.

## Setup

### 1. Pick a secret

```bash
openssl rand -hex 32
```

Put it in `/etc/orclaw/secrets.env`:

```bash
GITHUB_WEBHOOK_SECRET=<the value you just generated>
```

Restart the dashboard service so it picks up the secret. With no secret
set, `/webhook/github` returns `503` (disabled) — a misconfigured server
never silently accepts unsigned traffic.

### 2. Exclude the path from Cloudflare Access

In the Cloudflare Zero Trust dashboard, add an Access application (or a
policy on the existing one) with a **Bypass** rule for the exact path:

```
Path:   /webhook/github
Action: Bypass (Everyone)
```

Everything else under the dashboard hostname stays behind Access. Only
this one path is public — and it's authenticated by the HMAC signature.

### 3. Register the webhook on the repo

Repo → Settings → Webhooks → Add webhook:

- **Payload URL:** `https://<your-dashboard-host>/webhook/github`
- **Content type:** `application/json`
- **Secret:** the same value as `GITHUB_WEBHOOK_SECRET`
- **Events:** "Let me select individual events" → Issues, Issue comments,
  Pull requests, Pull request reviews, Pull request review comments,
  Workflow runs, Pushes. (Or "Send me everything" — the engine filters.)

GitHub will send a `ping`; you should see a `200 pong` in the Recent
Deliveries panel.

## Polling is still your safety net

Webhooks can be dropped (delivery failures, downtime, the race window in
the coalescing runner). The orchestrator timer keeps polling so nothing
is lost — you can *lengthen* the poll interval
(`ORCLAW_GITHUB_POLL_INTERVAL`) once webhooks are healthy, but don't turn
it off. Treat webhooks as the fast path and polling as reconciliation.

## Testing the endpoint by hand

```bash
SECRET=...   # same as GITHUB_WEBHOOK_SECRET
BODY='{"action":"created"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -X POST https://<host>/webhook/github \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
# → 202 {"status":"scheduled",...}
```

A wrong signature returns `401`; an unconfigured secret returns `503`.
