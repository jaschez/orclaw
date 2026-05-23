"""Web dashboard for orclaw.

A single-page FastAPI app exposing the engine's live state and a
controlled subset of write actions. Read by browsers behind Cloudflare
Access (cookie auth — works in browsers, unlike the MCP server which
needs OAuth).

Architecture: pure thin layer over the existing Python modules — same
queries used by the specialist tools and the CLI, just rendered as JSON
+ HTML instead of markdown tables.

Layout:
- ``app.py`` — FastAPI app + endpoints
- ``templates/index.html`` — single-page SPA
- ``static/`` — CSS + JS

Run via ``orclaw dashboard serve`` (added in cli.py).
"""

from __future__ import annotations
