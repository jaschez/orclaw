"""Spec-driven issue creation: turn a multi-issue markdown spec into
real GitHub issues, with codename → real-number substitution baked in.

The bug we want to make impossible: the specialist drops a spec into
``docs/business/specs/`` that references issues by codenames (``#P1``,
``#N1``), then the operator has to:

1. Manually create each issue on GitHub
2. Manually look up the real numbers
3. Manually substitute the codenames in every dependent body
4. Inevitably forget step 3 for some bodies, which silently breaks the
   planner (it sees ``Blocked by #P1`` and can't extract a number, so
   the dep vanishes and the dependent issue ends up in layer 0 next to
   its un-merged pre-req).

This tool parses the spec, computes the create order topologically, then
creates each issue with codenames already substituted with real numbers
so the planner sees clean ``Blocked by #194`` lines from the start.

Spec format (intentionally simple, no YAML parsing):

```markdown
## Issue: P1 — Provider entity + admin approval
<!-- labels: P0, area:backend, area:edge-fn, tests-required, agent:ready -->

### Contexto
Some description.

### Acceptance criteria
- Thing 1.

## Dependencias
(empty — root issue)

---

## Issue: P2 — REST API v1
<!-- labels: P0, area:edge-fn, tests-required, agent:ready -->

### Contexto
Builds on the provider tier.

## Dependencias
- Blocked by #P1 (provider tier scaffolding)
```

Sections are split by ``## Issue: <CODENAME> — <title>`` headings. The
body of each issue is everything until the next ``## Issue:`` (or EOF),
EXCLUDING the ``## Issue:`` line itself. The `<!-- labels: ... -->`
HTML comment sets the issue labels; multiple comma-separated. Codenames
are any uppercase-letter-prefixed identifier (``[A-Z][A-Z0-9_-]*``).
``Blocked by #<CODENAME>`` lines (and any other inline ``#<CODENAME>``
reference) are substituted with the real issue number after creation.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


# ``## Issue: P1 — Provider entity`` or with hyphen instead of em-dash
_ISSUE_HEADING_RE = re.compile(
    r"^##\s+Issue:\s+([A-Z][A-Z0-9_-]*)\s+[—\-]\s+(.+?)\s*$",
    re.MULTILINE,
)
_LABELS_RE = re.compile(r"<!--\s*labels:\s*(.+?)\s*-->", re.IGNORECASE)
# A codename token in body text: `#P1`, `#N1`, `#FOO_BAR`. Excludes
# anything where the leading `#` is preceded by a word char (so we don't
# accidentally substitute inside URLs like `https://a.b/c#P1`).
_CODENAME_REF_RE = re.compile(r"(?<![\w/])#([A-Z][A-Z0-9_-]*)\b")


@dataclass(frozen=True, slots=True)
class IssueSpec:
    codename: str
    title: str
    labels: list[str]
    body: str  # raw body with codenames still present
    deps: list[str]  # codenames this issue depends on (parsed from body)


def parse_spec(text: str) -> list[IssueSpec]:
    """Split a spec markdown into a list of IssueSpec.

    Raises ValueError if no issues found or codenames repeat.
    """
    headings = list(_ISSUE_HEADING_RE.finditer(text))
    if not headings:
        raise ValueError(
            "no `## Issue: <CODENAME> — <title>` sections found in spec"
        )

    specs: list[IssueSpec] = []
    seen: set[str] = set()

    for i, match in enumerate(headings):
        codename = match.group(1)
        title = match.group(2).strip()
        if codename in seen:
            raise ValueError(f"duplicate codename in spec: #{codename}")
        seen.add(codename)

        body_start = match.end() + 1  # skip newline after the heading
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[body_start:body_end].rstrip()

        # Strip any horizontal rule that's typically used to separate
        # spec sections so it doesn't end up in the issue body.
        body = re.sub(r"\n\s*-{3,}\s*$", "", body)

        labels_match = _LABELS_RE.search(body)
        if labels_match:
            labels = [
                lbl.strip()
                for lbl in labels_match.group(1).split(",")
                if lbl.strip()
            ]
            # Remove the labels comment from the body so it doesn't
            # appear in the rendered issue.
            body = _LABELS_RE.sub("", body, count=1).rstrip()
        else:
            labels = []

        # Parse dependencies: every #<CODENAME> reference anywhere in the
        # body counts as a dep. We use the full body (not just the
        # "Dependencias" section) so cross-references like "see #P3 for
        # detail" also become deps. Operator can always rewrite the
        # body if a reference is documentation-only.
        dep_codenames = sorted({m.group(1) for m in _CODENAME_REF_RE.finditer(body)})

        specs.append(
            IssueSpec(
                codename=codename,
                title=title,
                labels=labels,
                body=body.strip(),
                deps=dep_codenames,
            )
        )

    # Validate that every dep references a codename defined in this spec.
    all_codenames = {s.codename for s in specs}
    for s in specs:
        unknown = [d for d in s.deps if d not in all_codenames]
        if unknown:
            raise ValueError(
                f"#{s.codename} references unknown codename(s) in spec: "
                f"{', '.join('#' + u for u in unknown)}. "
                "Either define those issues in the same spec or fix the typo."
            )

    return specs


def topological_order(specs: list[IssueSpec]) -> list[IssueSpec]:
    """Return specs in an order safe to create (deps before dependents).

    Raises ValueError on cycle.
    """
    by_codename = {s.codename: s for s in specs}
    in_degree = {s.codename: len(s.deps) for s in specs}
    # blocker -> dependents
    edges: dict[str, list[str]] = {s.codename: [] for s in specs}
    for s in specs:
        for d in s.deps:
            edges[d].append(s.codename)

    ordered: list[IssueSpec] = []
    ready = sorted(c for c, deg in in_degree.items() if deg == 0)

    while ready:
        # Stable: alphabetical within a layer
        codename = ready.pop(0)
        ordered.append(by_codename[codename])
        for dependent in edges[codename]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(ordered) != len(specs):
        cycle = sorted(c for c, deg in in_degree.items() if deg > 0)
        raise ValueError(
            f"dependency cycle in spec involving: {', '.join('#' + c for c in cycle)}"
        )

    return ordered


def _substitute_codenames(body: str, mapping: dict[str, int]) -> str:
    """Replace #<CODENAME> with #<number> for every known mapping."""
    def repl(match: re.Match[str]) -> str:
        codename = match.group(1)
        if codename in mapping:
            return f"#{mapping[codename]}"
        return match.group(0)
    return _CODENAME_REF_RE.sub(repl, body)


def _gh_create_issue(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str],
) -> int:
    """Create an issue via gh CLI and return its number.

    Raises subprocess.CalledProcessError on gh failure.
    """
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    for lbl in labels:
        cmd.extend(["--label", lbl])
    proc = subprocess.run(
        cmd,
        input=body,
        capture_output=True,
        text=True,
        check=True,
    )
    # gh prints the URL on stdout: https://github.com/owner/repo/issues/N
    url = proc.stdout.strip().splitlines()[-1]
    match = re.search(r"/issues/(\d+)", url)
    if not match:
        raise RuntimeError(f"could not parse issue number from gh output: {url!r}")
    return int(match.group(1))


def create_issues_from_spec(
    spec_path: str,
    target_repo: str,
    dry_run: bool = False,
) -> str:
    """Parse ``spec_path`` and create the issues it declares in ``target_repo``.

    Workflow:

    1. Read + parse the spec into IssueSpec records (titles, labels, deps).
    2. Validate codenames + dep graph (no cycles, no unknown refs).
    3. Topologically sort so deps are created before dependents.
    4. For each issue (in order):
       - Substitute already-created ``#<CODENAME>`` → ``#<real_number>`` in
         the body. This guarantees the planner sees clean
         ``Blocked by #194`` lines from the moment the issue is created.
       - Create the issue via ``gh issue create`` with labels.
       - Record codename → real_number for the next iteration.
    5. Return a markdown summary with the codename → real-number mapping.

    Parameters
    ----------
    spec_path:
        Path to the markdown spec on disk (typically inside
        ``docs/business/specs/``).
    target_repo:
        ``owner/repo`` slug for the target repo. The ``gh`` CLI
        must already be authenticated with create-issues permission on
        this repo.
    dry_run:
        If true, parse + topologically-sort + report what WOULD be
        created, but make no GitHub calls. Useful for the operator to
        eyeball before committing.

    Returns
    -------
    str
        A markdown summary block. On success lists each codename, its
        real issue number, and the GitHub URL.
    """
    try:
        text = open(spec_path, encoding="utf-8").read()
    except OSError as e:
        return f"❌ could not read spec file: {e}"

    try:
        specs = parse_spec(text)
    except ValueError as e:
        return f"❌ spec parse error: {e}"

    try:
        ordered = topological_order(specs)
    except ValueError as e:
        return f"❌ spec dependency error: {e}"

    lines = [
        f"## Issue plan for {target_repo}",
        f"Parsed {len(specs)} issue(s) from `{spec_path}`. Order:",
        "",
    ]
    for i, s in enumerate(ordered, 1):
        deps = ", ".join(f"#{d}" for d in s.deps) if s.deps else "—"
        lines.append(f"{i}. **#{s.codename}** — {s.title}  _(labels: {', '.join(s.labels) or '—'}; deps: {deps})_")

    if dry_run:
        lines.append("")
        lines.append("_dry-run — no issues created. Re-run with `dry_run=False` to commit._")
        return "\n".join(lines)

    # Create in order, substituting codenames as we go.
    mapping: dict[str, int] = {}
    failures: list[str] = []
    created_urls: dict[str, str] = {}

    for s in ordered:
        body_with_refs = _substitute_codenames(s.body, mapping)
        try:
            number = _gh_create_issue(
                repo=target_repo,
                title=s.title,
                body=body_with_refs,
                labels=s.labels,
            )
            mapping[s.codename] = number
            created_urls[s.codename] = f"https://github.com/{target_repo}/issues/{number}"
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            failures.append(f"  #{s.codename}: gh failed — {stderr}")
            break  # don't keep creating dependents if a pre-req failed
        except Exception as e:
            failures.append(f"  #{s.codename}: {type(e).__name__}: {e}")
            break

    lines.append("")
    lines.append("## Result")
    if mapping:
        lines.append(f"Created **{len(mapping)}** issue(s):")
        for codename, number in mapping.items():
            url = created_urls[codename]
            lines.append(f"  - `#{codename}` → [#{number}]({url})")
    if failures:
        lines.append("")
        lines.append("⚠️ Failures (stopped to avoid creating orphans):")
        lines.extend(failures)
        remaining = [s.codename for s in ordered if s.codename not in mapping]
        if remaining:
            lines.append(f"Not created: {', '.join('#' + r for r in remaining)}")
    if not failures and mapping:
        lines.append("")
        lines.append("Run `run_planner` to surface them in the orchestrator queue.")

    return "\n".join(lines)
