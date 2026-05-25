"""Tests for dependency parser."""

from __future__ import annotations

from orclaw.dependency_parser import (
    find_suspicious_blocked_lines,
    is_blocked_open,
    parse_blocked_by,
)


class TestParseBlockedBy:
    def test_returns_empty_set_for_none(self) -> None:
        assert parse_blocked_by(None) == frozenset()

    def test_returns_empty_set_for_empty_string(self) -> None:
        assert parse_blocked_by("") == frozenset()

    def test_returns_empty_set_when_no_match(self) -> None:
        assert parse_blocked_by("This is just an issue body.") == frozenset()

    def test_parses_single_blocker(self) -> None:
        body = "Blocked by #88"
        assert parse_blocked_by(body) == frozenset({88})

    def test_parses_multiple_blockers_on_separate_lines(self) -> None:
        body = "Blocked by #88\nBlocked by #142"
        assert parse_blocked_by(body) == frozenset({88, 142})

    def test_tolerates_dash_bullet_prefix(self) -> None:
        body = "- Blocked by #88\n* Blocked by #142"
        assert parse_blocked_by(body) == frozenset({88, 142})

    def test_case_insensitive(self) -> None:
        body = "blocked by #1\nBLOCKED BY #2\nBlocked By #3"
        assert parse_blocked_by(body) == frozenset({1, 2, 3})

    def test_tolerates_extra_whitespace(self) -> None:
        body = "   Blocked by  #88  "
        assert parse_blocked_by(body) == frozenset({88})

    def test_tolerates_parenthetical_context(self) -> None:
        # Real-world issue bodies often append a (why) annotation after the
        # number. Previously the regex required \s*$ after the digits and
        # silently dropped every such line, causing the planner to flatten
        # every issue into layer 0 and dispatch dependents in parallel
        # with their pre-reqs.
        body = "- Blocked by #194 (necesita organization_id y embed_allowed_domains)"
        assert parse_blocked_by(body) == frozenset({194})

    def test_tolerates_trailing_period(self) -> None:
        body = "- Blocked by #201."
        assert parse_blocked_by(body) == frozenset({201})

    def test_tolerates_trailing_comment(self) -> None:
        body = "- Blocked by #88 — fix shipping first"
        assert parse_blocked_by(body) == frozenset({88})

    def test_ignores_inline_mentions(self) -> None:
        # "Blocked by #88" must be on its own line. Inline mentions like
        # "see blocked by #88 below" should not match.
        body = "See blocked by #88 elsewhere in the issue."
        assert parse_blocked_by(body) == frozenset()

    def test_real_world_body(self) -> None:
        body = """
## Contexto

Adds the coupons table.

## Dependencias

- Blocked by #201
- Blocked by #202

## OPS relacionadas
"""
        assert parse_blocked_by(body) == frozenset({201, 202})


class TestFindSuspiciousBlockedLines:
    def test_empty_body_returns_empty_list(self) -> None:
        assert find_suspicious_blocked_lines(None) == []
        assert find_suspicious_blocked_lines("") == []

    def test_clean_body_returns_empty_list(self) -> None:
        body = "## Dependencias\n- Blocked by #88\n- Blocked by #142"
        assert find_suspicious_blocked_lines(body) == []

    def test_flags_placeholder_codename(self) -> None:
        # The exact bug we hit: specialist writes `#P1` (a codename from
        # the spec) instead of substituting in the real issue number.
        # Regex rejects it because the digits are missing; without this
        # surface, the dep silently vanishes.
        body = "## Dependencias\n- Blocked by #P1 (provider tier scaffolding)"
        out = find_suspicious_blocked_lines(body)
        assert out == ["- Blocked by #P1 (provider tier scaffolding)"]

    def test_flags_missing_hash(self) -> None:
        body = "Blocked by issue 88"
        out = find_suspicious_blocked_lines(body)
        assert out == ["Blocked by issue 88"]

    def test_flags_multiple_offenders_separately(self) -> None:
        body = (
            "## Dependencias\n"
            "- Blocked by #N1 (multi-org schema)\n"
            "- Blocked by #194 (good one)\n"
            "- Blocked by #P3 (bulk endpoints)\n"
        )
        out = find_suspicious_blocked_lines(body)
        # #194 line is clean; the two placeholders are suspicious.
        assert out == [
            "- Blocked by #N1 (multi-org schema)",
            "- Blocked by #P3 (bulk endpoints)",
        ]

    def test_does_not_flag_clean_annotated_lines(self) -> None:
        # The regex now accepts annotations; clean parses should NOT
        # be flagged as suspicious.
        body = (
            "- Blocked by #194 (needs multi-org schema)\n"
            "- Blocked by #201."
        )
        assert find_suspicious_blocked_lines(body) == []


class TestIsBlockedOpen:
    def test_returns_false_when_no_deps(self) -> None:
        assert not is_blocked_open(1, frozenset(), frozenset({2, 3}))

    def test_returns_false_when_all_deps_closed(self) -> None:
        # deps are 88 and 142, neither is open
        assert not is_blocked_open(1, frozenset({88, 142}), frozenset())

    def test_returns_true_when_any_dep_open(self) -> None:
        assert is_blocked_open(
            issue_number=1,
            blocked_by=frozenset({88, 142}),
            open_issues=frozenset({88}),  # 88 still open
        )

    def test_returns_false_when_dep_not_in_open_set(self) -> None:
        # If 88 doesn't appear in open_issues, it's closed (or doesn't exist).
        assert not is_blocked_open(1, frozenset({88}), frozenset())
