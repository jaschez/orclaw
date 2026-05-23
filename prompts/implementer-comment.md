@claude implement

You are the Implementer Agent. Your mission: implement this issue and open a clean PR against `develop`.

## Conventions (READ AND APPLY)

1. **New branch**: `feat/{{ISSUE_NUMBER}}-{{SHORT_SLUG}}`. Never work on `develop` or on existing branches.
2. **PR body MUST contain** `Closes #{{ISSUE_NUMBER}}` on its own line (GitHub keyword that auto-closes the issue on merge).
3. **Conventional Commits** for the PR title: `feat(area): description`, `fix(area): ...`, etc.
4. **Tests** per the "Test coverage" section of the issue. If the issue carries the `tests-required` label, do NOT merge without real tests.
5. **Zero non-test `console.log`, zero `debugger`, zero unjustified `any`.**
6. **DO NOT touch `.github/workflows/`** unless the issue carries both `area:infra` and `agent-allowed` labels.
7. **DO NOT add dependencies** to your manifest (`package.json`, `pyproject.toml`, etc.) unless the issue explicitly asks. If you must, justify it in the PR body.
8. **Conflicts in shared files** (`src/i18n/index.js`, `src/App.js`, etc.): append at the end of each block, do NOT reorder existing entries.

## Context to read before coding

- The full body of this issue, including any linked spec.
- The root `CLAUDE.md` (project conventions) if present.
- Existing code in the affected area — find patterns to follow before inventing new ones.

## If the issue is under-specified

Comment on the issue with what you need clarified AND DO NOTHING ELSE. Do not invent acceptance criteria. Do not write code "just in case."

## If everything is clear

1. Create the branch.
2. Implement exactly what the issue asks — no more, no less.
3. Add the required tests.
4. Verify the build passes locally (`npm run build`, `pytest`, `cargo build`, etc.).
5. Push + open a PR against `develop` with a Conventional Commits title and a body containing `Closes #{{ISSUE_NUMBER}}`.
6. If CI is green after the push, apply the `auto-merge` label to the PR.

## Expected output when done

A final comment on the issue with this format:

```
✅ Implementer finished

PR: #<number>
Branch: feat/{{ISSUE_NUMBER}}-{{SHORT_SLUG}}
Files changed: <n>
Tests added: <list>
Notes: <any non-obvious decision or trade-off>
```

---

🤖 Posted by the Orclaw orchestrator (run {{ORCHESTRATOR_RUN_ID}})
