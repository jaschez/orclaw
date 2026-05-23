# Reviewer prompt — reference notes

The **live** template that gets posted to PRs is
[`reviewer-comment.md`](reviewer-comment.md). This file is a place to
keep your own design notes about how you want the reviewer agent to
behave.

The reviewer is what protects auto-merge from itself. Its decisions
fall into four buckets:

- **`review:approved`** — clean PR, `auto-merge` label applied.
- **`review:needs-changes`** — non-trivial issues, must be addressed
  before merge.
- **`review:minor-fixes-applied`** — reviewer fixed small things
  itself (typos, missing aria-label) and re-approved.
- **`review:hard-block`** — always-human-review (payments, migrations,
  workflows, large diffs).

Customize the hard checks + qualitative rubric in
`reviewer-comment.md` to match your team's bar. The shipped defaults
are deliberately strict — soften for indie projects, harden for
production work.
