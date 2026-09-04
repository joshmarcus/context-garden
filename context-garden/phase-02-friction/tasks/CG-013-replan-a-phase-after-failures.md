---
id: CG-013
title: Replan a phase after failures
status: draft
product: context-garden
phase: phase-02-friction
depends_on: [CG-008]
priority: 3
estimate: M
reading:
  - principles/agent-loop.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

`garden plan product/phase --replan` includes failed/blocked task logs and the friction doc in the planner prompt so it can propose fixes, splits, or new tasks.

## Context

`plan_prompt` in `src/garden/planner.py`. Add a section with, for each failed task: id, title, last three log lines, and the worker's blocked question if any. Keep the prompt under the brief budget by truncating logs.

## Acceptance criteria

- [ ] Prompt section appears only with `--replan`.
- [ ] Planner may output `"supersedes": [ids]`; import marks those tasks cancelled with a log line.
- [ ] Test the import path with a JSON fixture.

## Out of scope

- Automatic re-approval of replanned tasks.
