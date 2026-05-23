/**
 * Worker entrypoint — OAuth 2.1 in front, proxy to backend MCP behind.
 *
 * The OAuthProvider library handles all the spec-compliant OAuth endpoints
 * (/.well-known/oauth-protected-resource, /.well-known/oauth-authorization-server,
 * /authorize, /token, /register). We only wire two things:
 *
 *   - defaultHandler: serves /authorize and /callback (Access OIDC dance).
 *   - apiHandler: gets called AFTER the bearer token is validated. We
 *     strip the user-issued Bearer, inject the CF-Access service token
 *     headers, and forward the request to the backend MCP — the existing
 *     tunnel-backed orclaw-specialist.
 */

import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { AccessHandler } from "./access-handler";

interface Env {
  OAUTH_KV: KVNamespace;

  // Backend MCP (the existing tunnel-backed specialist).
  BACKEND_MCP_URL: string;
  CF_ACCESS_CLIENT_ID: string;
  CF_ACCESS_CLIENT_SECRET: string;

  // Cloudflare Access for SaaS as upstream OIDC IdP.
  ACCESS_AUTHORIZATION_URL: string;
  ACCESS_TOKEN_URL: string;
  ACCESS_JWKS_URL: string;
  ACCESS_AUD: string;
  ACCESS_CLIENT_ID: string;
  ACCESS_CLIENT_SECRET: string;
}

/**
 * Pure proxy: copy method, headers (minus the OAuth Bearer), and body
 * from the incoming request to the backend, adding the CF Access service
 * token headers so the backend's policy lets us through.
 */
async function proxyToBackend(
  request: Request,
  env: Env,
): Promise<Response> {
  const incoming = new URL(request.url);
  const backend = new URL(env.BACKEND_MCP_URL);

  // Preserve any extra path under /mcp (currently FastMCP exposes only
  // /mcp itself, but this is future-proof).
  if (incoming.pathname !== "/mcp" && incoming.pathname.startsWith("/mcp/")) {
    backend.pathname = backend.pathname + incoming.pathname.slice("/mcp".length);
  }
  backend.search = incoming.search;

  const headers = new Headers(request.headers);

  // The Authorization header was our OAuth Bearer — backend doesn't know it.
  headers.delete("Authorization");

  // The Host header would still point at the Worker; replace so the
  // tunnel hits the right hostname. (Backend has its own middleware that
  // rewrites Host to bind-host:port anyway, but keep this clean.)
  headers.set("Host", backend.host);

  // The service token that gets us past Cloudflare Access on the backend.
  headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);

  // CF removes hop-by-hop headers automatically, but be defensive.
  headers.delete("cf-connecting-ip");
  headers.delete("cf-ipcountry");
  headers.delete("cf-ray");
  headers.delete("cf-visitor");
  headers.delete("x-real-ip");
  headers.delete("x-forwarded-for");
  headers.delete("x-forwarded-proto");

  const init: RequestInit = {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? null : request.body,
    redirect: "manual",
  };

  return fetch(backend.toString(), init);
}

// The OAuthProvider exports a default fetch that:
//   1. Handles OAuth metadata + token endpoints internally.
//   2. For routes matching `apiRoute`, validates the Bearer token from
//      the request and only calls `apiHandler` if valid (otherwise 401
//      with proper Www-Authenticate header).
//   3. Everything else falls through to `defaultHandler`.
//
// OAuthProvider's TS types require `fetch` (not optional), so we inline
// the handler object literally — TS infers `{fetch}` which satisfies
// `ExportedHandlerWithFetch`.
export default new OAuthProvider({
  apiRoute: "/mcp",
  apiHandler: {
    fetch: (request: Request, env: unknown) => proxyToBackend(request, env as Env),
  },
  defaultHandler: {
    fetch: (request: Request, env: unknown, ctx: ExecutionContext) =>
      AccessHandler.fetch(request, env as Env, ctx),
  },
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
});
