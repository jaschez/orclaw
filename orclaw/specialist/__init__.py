"""Specialist agent — operate the engine via natural language.

Exposes the engine's operational surface as a set of MCP tools so a Claude
session (Claude Code locally, or claude.ai via remote MCP) can take action
on the engine without anyone SSHing into the box.

The architecture is intentionally simple:

- :mod:`orclaw.specialist.tools` — one Python function per
  operational action. Each function is a thin sync wrapper around the
  existing CLI / state / GitHub layer. They take typed args and return
  plain strings so MCP can render them.
- :mod:`orclaw.specialist.server` — FastMCP-based server. Just
  decorates each tool with ``@mcp.tool()`` so the schema + description
  are derived from type hints + docstrings.

Tools are deliberately conservative: no destructive action without an
explicit ``confirm=True`` flag where it matters.
"""

from __future__ import annotations
