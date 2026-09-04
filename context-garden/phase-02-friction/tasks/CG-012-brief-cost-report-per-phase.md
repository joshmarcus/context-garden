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

`build_brief` already returns per-section sizes. Add `garden cost [product/phase]` and a Cost tab in the web UI. Include actual usage from run records next to the estimates when available.

## Acceptance criteria

- [ ] CLI table: task, brief tokens (est), actual input tokens (last run), cost.
- [ ] Phase header line with the fixed-cost estimate.
- [ ] Web page with the same data.

## Out of scope

- Optimising the briefs automatically.
