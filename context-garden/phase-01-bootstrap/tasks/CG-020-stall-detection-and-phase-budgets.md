---
id: CG-020
title: "Stall detection and phase budgets"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-017, CG-016]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/coordination.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Stop revise loops that are not converging, and cap spend per phase.

## Acceptance criteria

- [x] Unchanged diff after a revise run, or a repeated blocking review finding -> `needs_human`; `garden retry` continues.
- [x] `budgets` / `budget_usd` pause dispatch with one `budget` event; `garden status` shows spent/budget.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
