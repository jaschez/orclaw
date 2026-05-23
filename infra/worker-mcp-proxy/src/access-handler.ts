/**
 * OAuth handler for Cloudflare Access for SaaS as the upstream OIDC provider.
 *
 * Flow:
 *   1. MCP client (claude.ai) calls /authorize on this Worker.
 *   2. We park the original OAuth request in KV (so we can resume after
 *      the IdP roundtrip) and redirect the user to Cloudflare Access's
 *      authorization URL.
 *   3. User logs in via the identity provider configured behind Access.
 *   4. Access redirects to our /callback with a `code`.
 *   5. We exchange the code for an id_token, decode it, and call
 *      `completeAuthorization` on the OAuthProvider helpers — that issues
 *      the final Bearer token for the MCP client.
 *
 * We trust the id_token because we just received it over TLS from
 * Cloudflare Access. For stronger guarantees we'd verify the signature
 * against ACCESS_JWKS_URL — overkill for our threat model (the Worker
 * itself is the only consumer and we control deploy).
 */

import type {
  AuthRequest,
  OAuthHelpers,
} from "@cloudflare/workers-oauth-provider";
import { Hono } from "hono";

export interface Env {
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers;

  ACCESS_AUTHORIZATION_URL: string;
  ACCESS_TOKEN_URL: string;
  ACCESS_JWKS_URL: string;
  ACCESS_AUD: string;

  ACCESS_CLIENT_ID: string;
  ACCESS_CLIENT_SECRET: string;
}

interface IdTokenPayload {
  sub: string;
  email?: string;
  name?: string;
  aud?: string | string[];
  iss?: string;
}

// We use Hono for nice routing + we'll also need a couple of HTML responses.
const app = new Hono<{ Bindings: Env }>();

/**
 * /authorize — entrypoint from the MCP client (claude.ai).
 *
 * We don't actually authenticate here; we just shuttle the user to
 * Cloudflare Access, parking the original request in KV so the callback
 * can complete it.
 */
app.get("/authorize", async (c) => {
  const oauthReqInfo = await c.env.OAUTH_PROVIDER.parseAuthRequest(c.req.raw);
  if (!oauthReqInfo.clientId) {
    return c.text("Invalid OAuth request: missing client_id", 400);
  }

  // Persist the request info under a random key so the callback can find it.
  const stateKey = crypto.randomUUID();
  await c.env.OAUTH_KV.put(
    `oauth-state:${stateKey}`,
    JSON.stringify(oauthReqInfo),
    { expirationTtl: 600 }, // 10 minutes — generous for the IdP roundtrip
  );

  const callbackUrl = new URL("/callback", c.req.url).toString();

  const authUrl = new URL(c.env.ACCESS_AUTHORIZATION_URL);
  authUrl.searchParams.set("client_id", c.env.ACCESS_CLIENT_ID);
  authUrl.searchParams.set("redirect_uri", callbackUrl);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("scope", "openid email profile");
  authUrl.searchParams.set("state", stateKey);

  return c.redirect(authUrl.toString(), 302);
});

/**
 * /callback — Access redirects here after the user logs in.
 */
app.get("/callback", async (c) => {
  const code = c.req.query("code");
  const stateKey = c.req.query("state");

  if (!code || !stateKey) {
    return c.text("Missing code or state in callback", 400);
  }

  // Retrieve the parked OAuth request.
  const rawParked = await c.env.OAUTH_KV.get(`oauth-state:${stateKey}`);
  if (!rawParked) {
    return c.text("State expired or unknown — please restart the OAuth flow", 400);
  }
  await c.env.OAUTH_KV.delete(`oauth-state:${stateKey}`);
  const oauthReqInfo = JSON.parse(rawParked) as AuthRequest;

  // Exchange the code for tokens.
  const callbackUrl = new URL("/callback", c.req.url).toString();
  const tokenResp = await fetch(c.env.ACCESS_TOKEN_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      client_id: c.env.ACCESS_CLIENT_ID,
      client_secret: c.env.ACCESS_CLIENT_SECRET,
      redirect_uri: callbackUrl,
    }),
  });

  if (!tokenResp.ok) {
    const body = await tokenResp.text();
    return c.text(
      `Access token exchange failed (${tokenResp.status}): ${body.slice(0, 500)}`,
      500,
    );
  }

  const tokens = (await tokenResp.json()) as {
    id_token?: string;
    access_token?: string;
  };
  if (!tokens.id_token) {
    return c.text("No id_token in Access response", 500);
  }

  const payload = decodeJwt<IdTokenPayload>(tokens.id_token);
  if (!payload) {
    return c.text("Invalid id_token format", 500);
  }

  // Light AUD check: we expect the token to be issued for OUR app.
  const audOk =
    typeof payload.aud === "string"
      ? payload.aud === c.env.ACCESS_AUD
      : Array.isArray(payload.aud) && payload.aud.includes(c.env.ACCESS_AUD);
  if (!audOk) {
    return c.text(
      `id_token aud (${JSON.stringify(payload.aud)}) does not match ACCESS_AUD`,
      403,
    );
  }

  const email = payload.email ?? `${payload.sub}@access`;

  // Issue the OAuth token to the MCP client. props is opaque metadata
  // we can read back inside the proxy handler if we ever need to log
  // who's calling which tool.
  const { redirectTo } = await c.env.OAUTH_PROVIDER.completeAuthorization({
    request: oauthReqInfo,
    userId: payload.sub,
    metadata: { label: email },
    scope: oauthReqInfo.scope,
    props: { email, sub: payload.sub },
  });

  return c.redirect(redirectTo, 302);
});

/**
 * Minimal landing page in case someone hits the root.
 */
app.get("/", (c) =>
  c.html(
    "<h1>orclaw-mcp-proxy</h1>" +
      "<p>OAuth-fronted MCP proxy. The active endpoint is <code>/mcp</code>.</p>",
  ),
);

/**
 * /.well-known/oauth-protected-resource (RFC 9728).
 *
 * The MCP spec 2025-06-18 wants servers to expose this endpoint so MCP
 * clients can discover the OAuth Authorization Server URL without
 * relying on the older `oauth-authorization-server` convention. Our
 * library version (workers-oauth-provider 0.0.5) doesn't ship this
 * endpoint, so we add it manually — it's just a static JSON manifest.
 */
app.get("/.well-known/oauth-protected-resource", (c) => {
  const base = new URL(c.req.url).origin;
  return c.json({
    resource: `${base}/mcp`,
    authorization_servers: [base],
    bearer_methods_supported: ["header"],
    resource_documentation: base,
    scopes_supported: ["openid", "email", "profile"],
  });
});

// Note: clients sometimes probe `/mcp/.well-known/oauth-protected-resource`
// per RFC 9728 §3.2, but our /mcp route is owned by OAuthProvider which
// 401's anything under it. The base /.well-known above is sufficient —
// the OAuth spec allows that fallback location.

function decodeJwt<T>(token: string): T | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    // JWT base64url, not standard base64.
    const padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const decoded = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
    return JSON.parse(decoded) as T;
  } catch {
    return null;
  }
}

export { app as AccessHandler };
