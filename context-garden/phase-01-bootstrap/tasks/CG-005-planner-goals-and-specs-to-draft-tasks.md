---
id: CG-005
title: "Planner: goals and specs to draft tasks"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-001]
priority: 1
estimate: M
reading:
  - context-garden/phase-01-bootstrap/specs/task-format.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

One-call planning that turns a phase's goals and specs into draft task files, plus `--import` for plans produced by a human-driven session.

## Acceptance criteria

- [x] `garden plan product/phase --dry-run` prints the prompt and its token estimate.
- [x] Batch-internal dependencies referenced by title resolve to ids.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
