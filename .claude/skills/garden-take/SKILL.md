---
name: garden-take
description: Take a task from the context garden queue into this interactive session, do the work, and hand the result back so the garden pushes the branch and opens the PR. Use when the user says "take CG-012", "/garden-take <id>", or "pick up the next ready task".
---

# garden-take

You are acting as a garden worker inside an interactive Claude Code session. The garden
(`garden` CLI, config in `garden.yaml` at the repo root or a parent) owns task state; you
own the code change.

## Steps

1. Pick the task. If the user gave an id, use it. Otherwise run `garden ready` and take the
   first line (highest priority, unblocked).
2. Claim it and get the brief:

   ```bash
   garden take <ID> --worktree -q      # prints the brief path; creates .garden/worktrees/<ID>
   ```

   Read the brief file it printed. It is the complete specification: operating rules,
   principles digest, product overview, phase goals, the task, and the reading list.
   If the reading list says "read these", read those files too. Do not explore the
   context garden beyond that.
3. `cd` into the worktree path shown by `garden take` (`.garden/worktrees/<ID>`), which is
   already on the task branch. Do the work there. Commit in small steps. Do NOT push and
   do NOT open a PR.
4. Run the project's checks named in the product overview. Fix what you broke.
5. Hand back the result. Write the same JSON the brief asks for and pass it to `finish`:

   ```bash
   garden finish <ID> --result '{"status": "done", "summary": "...", "pr_title": "...", "pr_body": "...", "notes": ""}'
   ```

   `finish` pushes the branch and opens the PR (or comments on the existing one for a
   revision round) and moves the task to `in_review`. If you are blocked on a human
   decision, use `garden finish <ID> --blocked --summary "<the precise question>"` instead.

## If you are already inside the product repo (no worktree)

Run `garden take <ID>` without `--worktree`, create the branch it names from the base
branch, work, push, open the PR yourself, then `garden finish <ID> --pr <url> --summary "..."`.

## Rules

- Never edit files under `**/tasks/` in the garden; the scheduler owns them.
- One task, one PR. Note follow-ups in `pr_body`, not in the diff.
- Put a `## Friction` section in `pr_body` listing anything the brief should have told you.
