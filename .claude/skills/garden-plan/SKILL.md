---
name: garden-plan
description: Plan a context-garden phase from this interactive session - turn goals and specs into draft task files without a separate headless model call. Use when the user says "plan phase-02", "/garden-plan product/phase", or "break this phase into tasks".
---

# garden-plan

`garden plan <product>/<phase>` normally makes one headless `claude -p` call. From an
interactive session you *are* the planner, so do the same work here and import the result.

## Steps

1. Get the planning prompt (it contains the rules, digest, product, goals, specs, docs and
   existing tasks):

   ```bash
   garden plan <product>/<phase> --dry-run > /tmp/garden-plan-prompt.md
   ```

   Read it in full. If the user gave extra guidance, add `--guidance "..."`.
2. Produce the JSON array the prompt asks for. Write it to `/tmp/garden-plan.json`.
   Rules that matter most:
   - 3-12 tasks, each one PR, each shippable on its own.
   - `depends_on` only for real ordering constraints; reference other tasks in the batch
     by exact title.
   - `reading` lists only the garden files the worker needs (digest/product/goals are
     automatic).
   - Body has `## Goal`, `## Context`, `## Acceptance criteria` (testable checklist),
     `## Out of scope`.
3. Import:

   ```bash
   garden plan <product>/<phase> --import /tmp/garden-plan.json
   ```

   Tasks are created as `draft`. Show the user `garden ls -p <product> --phase <phase>` and
   `garden graph --phase <phase>`, then ask which to approve (`garden approve <ID>...` or
   `garden approve --all <product>/<phase>`).

## Replanning

If tasks failed or came back blocked, read their logs first (`garden show <ID>`), then
plan only the gap: smaller tasks, clearer reading lists, or a spec fix. Prefer fixing the
spec over adding a task that explains the spec.
