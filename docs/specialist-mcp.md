# Specialist — operating the engine via Claude (MCP)

The specialist exposes the operational cheatsheet as an MCP tool catalog.
Once connected, you talk to Claude in natural language ("pause the
engine", "what failed today?", "skip issue 99 because it's blocked")
and Claude picks the right tool, fills the args, and reports back.

The same 14 tools are reachable from two places:

- **Locally**, via Claude Code on your laptop — stdio transport, zero
  network exposure.
- **Remotely**, via Claude Code on your phone or claude.ai — HTTP-SSE
  transport, exposed through Cloudflare Tunnel + Zero Trust.

## Tool catalog

Same actions as the cheatsheet in `operations.md`, but Claude picks the
right one from a natural-language request:

**Read-only (always safe):**

| Tool | What it returns |
|---|---|
| `get_status` | Pause flag, in-flight count, batch + run counts |
| `get_decision_preview` | What the next tick *would* dispatch (dry-run) |
| `get_recent_logs(lines, unit)` | journalctl tail |
| `query_runs(limit, agent, status)` | Filtered run rows |
| `query_batches(limit, status)` | Filtered batch rows |
| `get_summary(window_days)` | Daily/weekly digest |
| `doctor` | Full diagnostic table |

**Actions (mutate state):**

| Tool | Effect |
|---|---|
| `pause_orchestrator` | Sets `engine_state.orchestrator_paused=true` |
| `resume_orchestrator` | Clears the pause flag |
| `force_tick` | Runs one tick in apply mode now |
| `run_planner` | Re-scans GitHub, recomputes layers |
| `skip_issue(issue_number, reason)` | Adds `do-not-implement` label + planner refresh |
| `require_human_review(pr_number, reason)` | Adds `requires-human-review` label to PR |
| `force_review(pr_number)` | Strips `review:*` labels so the reviewer re-dispatches |

The first few tools (read-only) are safe — Claude will use them freely to
investigate before acting. The action tools post real GitHub comments
and labels; the system prompt instructs Claude to confirm with you on
non-obvious cases.

---

## Local mode (Claude Code on your machine)

The simplest setup. Claude Code launches the specialist as a subprocess
over stdio. No network, no auth headaches.

### 1. Install the specialist dependencies

In the engine's venv:

```bash
cd /opt/orclaw   # or wherever you cloned it locally
source .venv/bin/activate
pip install -e '.[specialist]'   # picks up the optional `mcp` dep
```

### 2. Register the MCP server in Claude Code

Edit `~/.claude/mcp_servers.json` (create it if missing):

```json
{
  "mcpServers": {
    "orclaw": {
      "command": "/opt/orclaw/.venv/bin/orclaw",
      "args": ["specialist", "serve", "--transport=stdio"],
      "env": {
        "GITHUB_TOKEN": "ghp_...",
        "GITHUB_REPO": "${TARGET_REPO}"
      }
    }
  }
}
```

If you're running locally and the engine talks to the *production* SQLite
(via SSH-mounted FS or rsync'd copy), point `ORCLAW_DATA_DIR` at the right
spot too — by default tools read from `/var/lib/orclaw/data`.

### 3. Restart Claude Code and try it

```
You: ¿cómo va el motor?
Claude: [calls get_status] El motor está activo, sin pausa, con 0 runs
        en vuelo. Hay 5 batches pendientes en el layer 0 y 12 ya merged.
```

---

## Remote mode (móvil / claude.ai / cualquier Claude Code)

For operating the engine from your phone or a laptop that isn't the
engine host. Two pieces:

1. The specialist as a long-running HTTP-SSE server on the engine VM
2. Cloudflare Tunnel + Zero Trust gating the public URL

### 1. Enable the specialist service on the engine VM

```bash
sudo systemctl enable --now orclaw-specialist.service
journalctl -u orclaw-specialist -f
```

Confirm it's listening locally:

```bash
curl -s http://127.0.0.1:8765/mcp
# Should respond (with an MCP error — but TCP-level connection works)
```

### 2. Add the route to your Cloudflare Tunnel

In your tunnel config (`~/.cloudflared/config.yml` on the VM, or the
Cloudflare dashboard if you use the GUI):

```yaml
ingress:
  - hostname: orclaw.<YOUR_TEAM>.com
    path: /mcp
    service: http://localhost:8765
  - hostname: orclaw.<YOUR_TEAM>.com
    service: http_status:404
```

Restart the tunnel:

```bash
sudo systemctl restart cloudflared
```

### 3. Lock it down with Zero Trust

In **Cloudflare Zero Trust dashboard → Access → Applications → Add**:

- **Type:** Self-hosted
- **Application domain:** `orclaw.<YOUR_TEAM>.com`
- **Path:** `/mcp`
- **Policy:** allow only your specific email or GitHub identity. Reject
  everything else.

Without this step the URL is *world-readable* and anyone hitting it can
operate your engine. **Don't skip.**

### 4. Connect from any Claude Code

On your phone / laptop / wherever:

```json
{
  "mcpServers": {
    "orclaw-remote": {
      "url": "https://orclaw.<YOUR_TEAM>.com/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Claude Code will prompt for the Cloudflare Access SSO when it first
connects. After that, all 14 tools are available.

---

## Verifying the tool catalog

Run the build smoke-test (doesn't actually start a transport):

```bash
/opt/orclaw/.venv/bin/python -c \
  "from orclaw.specialist.server import build_server; \
   s = build_server(); \
   print('tools:', sorted(t.name for t in s._tool_manager.list_tools()))"
```

Should print exactly 14 tools, alphabetically.

---

## Security model — what the specialist can and can't do

✅ **Can:**
- Read everything: DB, logs, GitHub PR/issue list
- Toggle the pause flag
- Force a tick / re-run the planner
- Add labels (`do-not-implement`, `requires-human-review`, `auto-merge`-related)
- Post comments authored as the PAT owner

❌ **Cannot:**
- Delete data (no `DROP TABLE`, no `git push --force`)
- Change config / rotate secrets / restart systemd
- Implement code itself — it dispatches the implementer; doesn't write code
- Bypass the reviewer's hard-block — `force_review` re-queues, doesn't auto-approve

If you ever want to extend it (e.g., add an `update_concurrency_cap` tool),
edit `orclaw/specialist/tools.py`, add the function, register it in
`server.build_server()`, write a test, restart the systemd unit. Schema +
description are derived from the type hints and docstring automatically.

---

## Troubleshooting

- **Claude says "no tools available"** → restart Claude Code after editing
  `mcp_servers.json`. It only reads it on startup.
- **`orclaw specialist serve` exits with ImportError** → install
  the optional dep: `pip install -e '.[specialist]'`.
- **Remote URL returns 401/403** → Cloudflare Zero Trust policy is
  blocking you. Check Access logs in the CF dashboard.
- **A tool returns "Config error: GITHUB_TOKEN not set"** → for stdio
  mode, add the env block to `mcp_servers.json`. For HTTP mode, the
  systemd unit already loads `/etc/orclaw/secrets.env`.
- **`get_recent_logs` returns "journalctl not available"** → expected if
  you're running the specialist on macOS for local testing; works on
  the engine VM.
