# Specialist prompt — reference notes

The specialist is the persona you talk to from claude.ai mobile/web,
Claude Code, or any MCP client, connected to the Orclaw MCP server at
`<your-domain>/mcp`.

Unlike the implementer/reviewer agents that run as `@claude` mentions
on GitHub, the specialist is a **conversational** agent — you ask it
"what's the status?" or "skip issue #42" and it uses the MCP tools to
read engine state and apply changes.

See [`specialist-mode.md`](specialist-mode.md) for a self-contained
prompt you can paste into a Claude Project's custom instructions so
the agent acts as the specialist persona.

For setup (Cloudflare Access OAuth, connecting from claude.ai), see
[`docs/specialist-mcp.md`](../docs/specialist-mcp.md).
