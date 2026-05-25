# Reviewer prompt — reference notes

The **live** template that gets posted to PRs is
[`reviewer-comment.md`](reviewer-comment.md). This file is a place to
keep your own design notes about how you want the reviewer agent to
behave.

The reviewer is what protects auto-merge from itself. Its decisions
fall into four buckets:

- **`review:approved`** — clean PR, `auto-merge` label applied.
- **`review:needs-changes`** — non-trivial issues, must be addressed
  before merge. The agent itself decides this (no human in the loop).
- **`review:minor-fixes-applied`** — reviewer fixed small things
  itself (typos, missing aria-label) and re-approved.
- **`review:hard-block` + `requires-human-review`** — escalation to a
  human. By design this is an **exception, not a rule**. The reviewer
  only applies it when it literally cannot execute the decision itself
  (irresoluble merge conflict needing product info, CI red whose fix
  is out of scope, diff that contradicts a recorded ADR).
  `requires-human-review` is otherwise reserved for the operator to
  pre-flag a PR out of the auto flow.

Specifically NOT grounds for escalation (the reviewer decides all of
these on its own):

- Large PRs (file count, line count).
- Migrations / schema changes.
- Payment-flow code (Stripe webhooks, etc.).
- CI workflow tweaks.
- Red CI (see the "CI failures: how to decide" section in the live
  prompt for the three-tier triage: minor-fixes → needs-changes →
  human, only ever in that order).

The shipped defaults bias toward shipping. Customize the hard checks
+ qualitative rubric in `reviewer-comment.md` to match your team's
bar — soften for indie projects, harden for production work — but
think twice before adding more auto-escalation rules. The point of
the engine is to deliver autonomously; every auto-escalation puts the
human back in the critical path.
