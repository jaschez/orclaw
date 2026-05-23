# Implementer prompt — reference notes

The **live** template that gets posted to issues is
[`implementer-comment.md`](implementer-comment.md). This file is a
place to keep your own design notes about how you want the implementer
agent to behave.

A typical reason to edit `implementer-comment.md` is to encode
project-specific conventions:

- Branch naming (`feat/`, `chore/`, `fix/` prefixes; ticket-id format).
- Commit-message format (Conventional Commits is the default).
- "Do not touch" paths (e.g. don't modify migrations, don't touch
  payment code without explicit approval).
- Test-coverage expectations (per-PR test count, fixture conventions).
- Style guide pointers (links to `CLAUDE.md`, ADRs, RFCs).

Customize freely. Dashboard edits to `implementer-comment.md` land in
the overlay layer and take effect on the very next orchestrator tick
without a restart.
