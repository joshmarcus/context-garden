---
name: garden-review
description: Review a context-garden task's pull request against its brief and acceptance criteria, and post the review on GitHub so the scheduler dispatches a revision round. Use when the user says "review CG-012", "/garden-review <id>", or "check the PR for this task".
---

# garden-review

Review a garden task's PR the way a careful maintainer would, with the task brief as the
contract.

## Steps

1. Load the contract: `garden show <ID>` (status, PR url, acceptance criteria) and
   `garden brief <ID> --no-rules` (the exact context the worker had).
2. Fetch the change: `gh pr diff <number>` and `gh pr view <number> --json body,title`.
   Check the PR body's "Friction" section; anything there is a spec/task fix to suggest to
   the user, not something to block the PR on.
3. Judge, in this order:
   - Does every acceptance criterion hold? Point at the evidence (test, code) or its absence.
   - Correctness and safety of the diff itself.
   - Scope: anything outside the task? Anything the task asked for that is missing?
   - Principles digest violations (tests skipped, scope widened, history rewritten).
4. Post the review with `gh pr review <number> --comment -b "..."` (or
   `--request-changes`). Be specific: file, line, what and why. The scheduler treats every
   new non-bot comment as feedback and will dispatch a `revise` run on the next tick
   (bounded by `max_revisions`), so only post what you want acted on.
5. If the PR is good, say so to the user and stop. Do NOT merge; merging is the human's
   decision. Once merged, the scheduler marks the task `done` and unblocks dependents.
