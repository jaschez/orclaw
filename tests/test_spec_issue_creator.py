"""Tests for spec_issue_creator (no GitHub I/O — pure parse + topology)."""

from __future__ import annotations

import pytest

from orclaw.spec_issue_creator import (
    _substitute_codenames,
    parse_spec,
    topological_order,
)


SAMPLE_SPEC = """\
# Spec: Provider tier rollout

## Issue: P1 — Provider entity + admin approval
<!-- labels: P0, area:backend, area:edge-fn, tests-required, agent:ready -->

### Contexto

Provider tier scaffolding.

## Dependencias
(none — root issue)

---

## Issue: P2 — REST API v1
<!-- labels: P0, area:edge-fn, tests-required, agent:ready -->

### Contexto

Builds on the provider tier.

## Dependencias
- Blocked by #P1 (provider tier scaffolding)

---

## Issue: P3 — Private /provider/docs page
<!-- labels: P0, area:frontend, tests-required, agent:ready -->

### Contexto

OpenAPI viewer + guides.

## Dependencias
- Blocked by #P2 (needs openapi.yaml)
"""


class TestParseSpec:
    def test_extracts_three_issues_from_sample(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        assert [s.codename for s in specs] == ["P1", "P2", "P3"]
        assert specs[0].title == "Provider entity + admin approval"
        assert specs[1].title == "REST API v1"
        assert specs[2].title == "Private /provider/docs page"

    def test_extracts_labels(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        assert specs[0].labels == [
            "P0",
            "area:backend",
            "area:edge-fn",
            "tests-required",
            "agent:ready",
        ]
        assert specs[1].labels == [
            "P0",
            "area:edge-fn",
            "tests-required",
            "agent:ready",
        ]

    def test_strips_labels_comment_from_body(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        for s in specs:
            assert "<!-- labels" not in s.body

    def test_strips_section_separator_from_body(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        # The `---` between sections should not leak into the body.
        for s in specs:
            assert not s.body.rstrip().endswith("---")

    def test_collects_dep_codenames(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        assert specs[0].deps == []
        assert specs[1].deps == ["P1"]
        assert specs[2].deps == ["P2"]

    def test_collects_inline_codename_refs_as_deps(self) -> None:
        spec = """\
## Issue: A — first
<!-- labels: P0 -->
nothing

## Issue: B — second
<!-- labels: P0 -->
See #A for context. Also depends on it.
"""
        specs = parse_spec(spec)
        assert specs[1].deps == ["A"]

    def test_rejects_duplicate_codenames(self) -> None:
        spec = "## Issue: P1 — first\nbody\n## Issue: P1 — second\nbody"
        with pytest.raises(ValueError, match="duplicate codename"):
            parse_spec(spec)

    def test_rejects_unknown_dep_ref(self) -> None:
        spec = """\
## Issue: P1 — first
<!-- labels: P0 -->
- Blocked by #XYZ
"""
        with pytest.raises(ValueError, match="unknown codename"):
            parse_spec(spec)

    def test_rejects_empty_spec(self) -> None:
        with pytest.raises(ValueError, match="no `## Issue:"):
            parse_spec("# Just a doc\n\nNothing here.")

    def test_does_not_substitute_inside_urls(self) -> None:
        # https://x/y#FOO should NOT be treated as a codename ref
        spec = """\
## Issue: P1 — first
<!-- labels: P0 -->
Some text https://example.com/docs#FOO is fine.
"""
        specs = parse_spec(spec)
        assert specs[0].deps == []


class TestTopologicalOrder:
    def test_orders_linear_chain(self) -> None:
        specs = parse_spec(SAMPLE_SPEC)
        ordered = topological_order(specs)
        assert [s.codename for s in ordered] == ["P1", "P2", "P3"]

    def test_orders_parallel_roots_alphabetically(self) -> None:
        spec = """\
## Issue: ZEBRA — z first
<!-- labels: P0 -->
body

## Issue: ALPHA — a second
<!-- labels: P0 -->
body
"""
        specs = parse_spec(spec)
        ordered = topological_order(specs)
        assert [s.codename for s in ordered] == ["ALPHA", "ZEBRA"]

    def test_detects_cycle(self) -> None:
        spec = """\
## Issue: A — first
<!-- labels: P0 -->
- Blocked by #B

## Issue: B — second
<!-- labels: P0 -->
- Blocked by #A
"""
        specs = parse_spec(spec)
        with pytest.raises(ValueError, match="cycle"):
            topological_order(specs)


class TestSubstituteCodenames:
    def test_replaces_known_codenames_only(self) -> None:
        body = "- Blocked by #P1\n- Blocked by #P2 (still placeholder)"
        out = _substitute_codenames(body, {"P1": 194})
        assert out == "- Blocked by #194\n- Blocked by #P2 (still placeholder)"

    def test_leaves_numeric_refs_alone(self) -> None:
        body = "see #88 elsewhere"
        out = _substitute_codenames(body, {"P1": 999})
        assert out == "see #88 elsewhere"

    def test_handles_multiple_substitutions(self) -> None:
        body = "- #A\n- #B\n- #C"
        out = _substitute_codenames(body, {"A": 1, "B": 2, "C": 3})
        assert out == "- #1\n- #2\n- #3"

    def test_does_not_touch_url_anchors(self) -> None:
        body = "doc at https://example.com/page#FOO is reference, not dep"
        out = _substitute_codenames(body, {"FOO": 99})
        # Not substituted (preceded by word char `/`)
        assert "https://example.com/page#FOO" in out
