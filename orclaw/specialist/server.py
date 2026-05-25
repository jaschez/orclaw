"""FastMCP server exposing the specialist tool catalogue.

Two transports are supported:

- **stdio**: the canonical MCP transport. A Claude Code session on the
  same machine launches the server as a subprocess and talks JSON-RPC
  over stdin/stdout. This is the right mode for local development.

- **streamable-http**: HTTP + SSE. Run the server as a long-lived
  process (systemd) and expose it through Cloudflare Tunnel. A Claude
  session on PC or mobile connects to ``https://orclaw.<YOUR_TEAM>.com/mcp``
  and gains all the tools without anyone touching SSH.

The actual tool implementations live in :mod:`orclaw.specialist.tools`
— this module is glue. FastMCP introspects each function's signature
and docstring to build the JSON schema + description that Claude sees,
so all the operator hints live where the code lives.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from orclaw.logging import get_logger
from orclaw.specialist import tools as t

log = get_logger(__name__)


def build_server() -> FastMCP:
    """Construct the MCP server with all specialist tools registered.

    Factored out of :func:`serve` so tests can introspect the resulting
    server without actually starting a transport.
    """
    mcp = FastMCP(
        name="orclaw-specialist",
        instructions=(
            "You operate the orclaw orchestrator on behalf of the user. "
            "Prefer read-only tools (get_status, query_runs, get_recent_logs) "
            "to understand the situation BEFORE calling action tools. Action "
            "tools (pause/resume/force_tick/skip_issue/require_human_review/"
            "force_review) post real comments and labels on GitHub — confirm "
            "with the user first when the action is destructive or non-obvious."
        ),
    )

    # --- read-only ---
    mcp.tool()(t.get_status)
    mcp.tool()(t.get_decision_preview)
    mcp.tool()(t.get_recent_logs)
    mcp.tool()(t.query_runs)
    mcp.tool()(t.query_batches)
    mcp.tool()(t.get_summary)
    mcp.tool()(t.doctor)
    mcp.tool()(t.query_events)
    mcp.tool()(t.view_engine_pr)

    # --- actions ---
    mcp.tool()(t.pause_orchestrator)
    mcp.tool()(t.resume_orchestrator)
    mcp.tool()(t.force_tick)
    mcp.tool()(t.run_planner)
    mcp.tool()(t.skip_issue)
    mcp.tool()(t.require_human_review)
    mcp.tool()(t.force_review)
    mcp.tool()(t.send_daily_summary)
    mcp.tool()(t.propose_engine_change)
    mcp.tool()(t.merge_engine_pr)

    # --- target-repo issue authoring ---
    # Turn a multi-issue spec .md into real GitHub issues on the target
    # repo, with codename -> real-number substitution baked in. Removes
    # the manual "create issues + look up numbers + edit dep refs" loop.
    from orclaw.spec_issue_creator import create_issues_from_spec

    mcp.tool()(create_issues_from_spec)

    return mcp


class _HostRewriteMiddleware:
    """ASGI middleware that rewrites the ``Host`` header so the inner
    Starlette/uvicorn stack always accepts the request.

    Why: behind Cloudflare Tunnel, requests arrive with
    ``Host: orclaw.<YOUR_TEAM>.com``. FastMCP's underlying transport
    rejects anything not matching the bound interface, returning
    ``421 Invalid Host header``.

    Implementation: synthesise the expected Host from the ASGI
    ``scope["server"]`` tuple ``(host, port)`` — guaranteed to match
    whatever uvicorn is bound to, INCLUDING the port. This fixes both
    Cloudflare-proxied traffic AND local-loopback probes (which were
    breaking in the first version that hardcoded the host without port).
    """

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
        if scope.get("type") == "http":
            server = scope.get("server")
            if server:
                bind_host, bind_port = server
                target = f"{bind_host}:{bind_port}".encode("ascii")
                new_headers = [
                    (name, target if name.lower() == b"host" else value)
                    for name, value in scope.get("headers", [])
                ]
                scope = {**scope, "headers": new_headers}
        await self.app(scope, receive, send)  # type: ignore[operator]


def serve(
    transport: Literal["stdio", "http"] = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Start the MCP server on the chosen transport.

    For stdio: blocks reading from stdin until the parent (Claude Code)
    closes the pipe. For http: starts a uvicorn server on ``host:port``
    serving the streamable-HTTP MCP endpoint at ``/mcp``.

    Defaults to binding 127.0.0.1 because the expectation is that you
    expose this via a reverse proxy (Cloudflare Tunnel) that handles
    auth — never directly to the public internet.
    """
    mcp = build_server()
    log.info("specialist_server_starting", transport=transport)

    if transport == "stdio":
        # FastMCP's run() handles stdio natively when called with no args.
        mcp.run()
    elif transport == "http":
        # Build FastMCP's Starlette ASGI app, wrap with Host rewrite, then
        # serve with uvicorn directly. mcp.run() would bypass our wrapper.
        import uvicorn

        starlette_app = mcp.streamable_http_app()
        wrapped = _HostRewriteMiddleware(starlette_app)
        uvicorn.run(wrapped, host=host, port=port, log_level="info")
    else:
        raise ValueError(f"Unknown transport: {transport!r}")
