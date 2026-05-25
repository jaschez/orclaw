"""Parse "Blocked by #N" lines from GitHub issue bodies.

This is the source of truth for the dependency graph. The format is
intentionally simple — we don't support arbitrary GitHub closing keywords,
only the explicit ``Blocked by #N`` line the specialist writes in each
issue body.

Regex tolerates:

- Case-insensitive ``Blocked by`` (also accepts ``blocked by``, ``BLOCKED BY``)
- Optional dash/bullet prefix (``- Blocked by #88``)
- Markdown list item formatting
- Trailing punctuation or parenthetical context after the issue number
  (``- Blocked by #194 (needs organization_id)``, ``- Blocked by #201.``).
  Previously the regex required ``\\s*$`` after the number which made it
  silently drop EVERY line that had any annotation — leading to the planner
  flattening everything into layer 0 and dispatching dependents before
  pre-reqs merged.
"""

from __future__ import annotations

import re

# Matches lines like:
#   Blocked by #88
#   - Blocked by #142
#   * blocked by  #99
#   - Blocked by #194 (needs organization_id and embed_allowed_domains)
#   - Blocked by #201.
# Captures the issue number. Word boundary after the digits so trailing
# punctuation, parenthetical context, or other commentary on the same line
# doesn't break the match.
_BLOCKED_BY_RE = re.compile(
    r"(?im)^[\s\-\*]*blocked\s+by\s+#(\d+)\b",
)

# Catches lines that LOOK like a dependency declaration but don't parse —
# e.g., placeholders the specialist forgot to substitute (``#N1``, ``#P3``),
# typos (``Blocked by issue 88``), or markdown that hides the ``#`` from us.
# Anything starting with bullet/whitespace + the word "blocked" that is NOT
# already a valid match is suspicious.
_BLOCKED_KEYWORD_RE = re.compile(
    r"(?im)^[\s\-\*]*blocked\s+by\b.*$",
)


def parse_blocked_by(body: str | None) -> frozenset[int]:
    """Return the set of issue numbers this body is blocked by.

    Returns an empty set for None or empty input.
    """
    if not body:
        return frozenset()
    return frozenset(int(m) for m in _BLOCKED_BY_RE.findall(body))


def find_suspicious_blocked_lines(body: str | None) -> list[str]:
    """Return lines that look like dependency declarations but don't parse.

    Useful for surfacing silent mal-formats in issue bodies — the bug we
    hit in production was the specialist writing ``Blocked by #P1`` (a
    placeholder codename instead of a real issue number). The regex
    correctly rejected those lines, but nothing surfaced the rejection to
    the operator, so every issue silently ended up with zero deps and the
    planner flattened the whole graph into layer 0.

    Returns the stripped text of each suspicious line so the caller can
    log them. Empty list for None / empty / clean bodies.
    """
    if not body:
        return []
    suspicious: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not _BLOCKED_KEYWORD_RE.match(line):
            continue
        if _BLOCKED_BY_RE.match(line):
            continue  # parsed cleanly
        suspicious.append(line)
    return suspicious


def is_blocked_open(
    issue_number: int,
    blocked_by: frozenset[int],
    open_issues: frozenset[int],
) -> bool:
    """True if any dep is still open.

    Used during batch planning: an issue is "ready" only when all its
    declared blockers are closed.
    """
    return any(dep in open_issues for dep in blocked_by)
