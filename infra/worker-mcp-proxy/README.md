# orclaw-mcp-proxy — Cloudflare Worker OAuth shim

A ~100 LOC Cloudflare Worker that fronts the existing orclaw
specialist MCP server with proper **OAuth 2.1 + PKCE**, so claude.ai
Custom Connectors (and any other OAuth-only MCP client) can connect.

## Why this exists

The specialist MCP runs as a Python process on the Oracle Cloud VM,
exposed via Cloudflare Tunnel at `https://orclaw.<YOUR_TEAM>.com/mcp`
and gated by Cloudflare Access (cookies + service token).

- **mcp-remote** (Claude Code) can authenticate with `CF-Access-Client-*`
  headers — works.
- **claude.ai web Custom Connector** cannot send custom headers and
  expects an OAuth 2.1 server with discovery — fails.

This Worker translates between the two:

```
claude.ai (OAuth 2.1) ──► Worker ──► tunnel ──► Python MCP (service token)
```

The Python tools stay untouched. The Worker is a stateless OAuth shim +
HTTP proxy. Single point of access for every MCP client; one auth model
for the whole house.

## Architecture

| Concern | Handled by |
|---|---|
| OAuth 2.1 server endpoints (`/authorize`, `/token`, `/.well-known/*`) | `@cloudflare/workers-oauth-provider` |
| User authentication | Cloudflare Access for SaaS as upstream OIDC IdP |
| Token storage | Workers KV namespace (`OAUTH_KV`) |
| MCP request proxy | `src/index.ts` — strips client Bearer, injects service token |
| Backend auth | Existing `CF-Access-Client-Id` / `Secret` service token |

## Deploy walkthrough

### Prerequisites

- Node.js ≥ 20 on your laptop (you already have 24)
- Wrangler installed (`npm i -g wrangler` or use `npx wrangler`)
- Authenticated Wrangler (`npx wrangler login` — opens browser)

### 1. Install dependencies

```bash
cd infra/worker-mcp-proxy
npm install
```

### 2. Create the KV namespace

```bash
npx wrangler kv namespace create OAUTH_KV
```

Output:

```
{
  "kv_namespaces": [
    { "binding": "OAUTH_KV", "id": "abc123def456..." }
  ]
}
```

Copy the `id` into `wrangler.toml` (replace `REPLACE_WITH_KV_NAMESPACE_ID`).

### 3. Create the Access for SaaS application

In **Cloudflare Zero Trust dashboard → Access → Applications**:

1. **Add an application → SaaS**
2. **Application name**: `Orclaw MCP Proxy`
3. **Application logo**: optional
4. **Authentication protocol**: **OIDC**
5. → **Next**
6. **Scopes**: tick `openid`, `email`, `profile`
7. **Redirect URLs**: add `https://orclaw-mcp-proxy.<your-cf-subdomain>.workers.dev/callback`
   (You'll know your `<subdomain>` after step 5; come back and add it then.)
8. → **Next**
9. **Add policy** → name `Only owner` → Allow → Emails: `<YOUR_EMAIL>`
10. → **Save**
11. **From the next page**, copy these values (you'll need them):
    - `Issuer` (e.g., `https://<team>.cloudflareaccess.com`)
    - `OIDC Discovery URL`
    - `Authorization endpoint`
    - `Token endpoint`
    - `JWKS endpoint`
    - `Client ID`
    - `Client Secret` (shown only once!)
    - `AUD` tag (often visible in the app's general settings)

### 4. Fill wrangler.toml

Replace the four `REPLACE_WITH_*` placeholders with the URLs + AUD from step 3.

### 5. Set the secrets

```bash
npx wrangler secret put ACCESS_CLIENT_ID
# paste the Client ID from step 3

npx wrangler secret put ACCESS_CLIENT_SECRET
# paste the Client Secret from step 3

npx wrangler secret put CF_ACCESS_CLIENT_ID
# paste your EXISTING service token Client ID (the one already used by mcp-remote)

npx wrangler secret put CF_ACCESS_CLIENT_SECRET
# paste the existing service token Client Secret
```

### 6. Deploy

```bash
npm run deploy
```

Output ends with something like:

```
Deployed orclaw-mcp-proxy to https://orclaw-mcp-proxy.<your-subdomain>.workers.dev
```

Copy the URL.

### 7. Go back to step 3.7 and add the redirect URL

The SaaS app needs to know the real callback URL. Add:

```
https://orclaw-mcp-proxy.<your-subdomain>.workers.dev/callback
```

(If you skipped this earlier, do it now — without it, OIDC will reject the redirect.)

### 8. Verify

```bash
curl -sI "https://orclaw-mcp-proxy.<your-subdomain>.workers.dev/.well-known/oauth-protected-resource"
# should return HTTP 200 with JSON body listing OAuth endpoints

curl -sI "https://orclaw-mcp-proxy.<your-subdomain>.workers.dev/mcp"
# should return HTTP 401 + Www-Authenticate header pointing at /authorize
```

If both look right, you're done.

### 9. Connect from claude.ai

**Settings → Connectors → Add custom connector**

- **Name**: `Orclaw`
- **Remote MCP server URL**: `https://orclaw-mcp-proxy.<your-subdomain>.workers.dev/mcp`

On first tool call, claude.ai will:
1. Detect the OAuth metadata
2. Open a popup to the Worker's `/authorize`
3. Redirect to Cloudflare Access login (one-time PIN to your email)
4. Redirect back to claude.ai with a Bearer token
5. Future calls use the token transparently for 24h (Access session)

## Optional — custom domain

If you want a memorable URL (e.g., `mcp.<YOUR_TEAM>.com`):

1. In `wrangler.toml`, uncomment the `routes` block and edit the pattern
2. In Cloudflare dashboard → DNS, create a CNAME `mcp` → `orclaw-mcp-proxy.<your-subdomain>.workers.dev`
   (or let `wrangler deploy` create it automatically when `custom_domain = true`)
3. Update the SaaS app redirect URL to `https://mcp.<YOUR_TEAM>.com/callback`
4. Update claude.ai connector to `https://mcp.<YOUR_TEAM>.com/mcp`

## Operating

| Action | Command |
|---|---|
| Deploy a change | `npm run deploy` |
| Tail live logs | `npm run tail` |
| Local dev (no backend) | `npm run dev` |
| Type-check | `npm run typecheck` |
| Rotate a secret | `npx wrangler secret put <NAME>` |
| List KV entries | `npx wrangler kv key list --binding=OAUTH_KV` |

## Security notes

- The Worker never sees user passwords — they go directly to Cloudflare Access.
- The service token (`CF_ACCESS_CLIENT_*`) gives the Worker (and only the
  Worker) backend access. The token is stored as a Worker secret, never
  in the Git repo.
- KV entries for OAuth state expire in 10 min; access tokens expire
  per the `@cloudflare/workers-oauth-provider` defaults (typically 1h
  with refresh).
- If a Worker secret leaks: rotate it with `npx wrangler secret put`,
  then the old token is invalidated on next deploy.

## Why not just put the tools in the Worker?

We thought about it. Two reasons we didn't:

1. **600 LOC of tools in Python** — porting to TypeScript would double
   the surface area to maintain.
2. **State lives on the VM** — the SQLite DB, `engine_state` flags,
   the tunnel to GitHub via the PAT. The tools need direct access to
   all of that; doing it via remote calls back to the VM would replicate
   the proxy pattern but inside-out and with worse latency.

The Worker stays a thin shim: OAuth on one side, HTTP on the other.

## When NOT to use this Worker

If you only operate the engine from:
- Claude Code on your PC (mcp-remote with service token headers — already works)
- SSH directly into the VM (CLI)

…then this Worker is overkill. It exists specifically to make
**browser-based MCP clients** (claude.ai web, future similar UIs) work.
