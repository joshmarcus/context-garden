---
id: CG-012
title: Brief cost report per phase
status: draft
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 3
estimate: M
difficulty: easy
reading:
  - principles/agent-loop.md
  - context-garden/phase-01-bootstrap/specs/brief.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Show, per phase, the fixed brief cost (digest + product + goals) and each task's reading-list cost, so the human can see when context is bloating.

## Context

`garden usage`, the task page and the phase table already show actual tokens and cost per task and per run; `build_brief` returns per-section sizes. Put the estimate next to the actual instead of adding a page: `garden usage` gains a brief-tokens column (fixed part plus reading list), the phase view gets a header line with the fixed-cost estimate, and the phase page's task table gets the same column.

## Acceptance criteria

- [ ] `garden usage product/phase`: task, brief tokens (estimated: fixed + reading), actual input tokens (last run), cost.
- [ ] A phase header line with the fixed-cost estimate, in the CLI and on the phase page.
- [ ] The phase page's task table carries the brief-tokens column.

## Out of scope

- Optimising the briefs automatically.
