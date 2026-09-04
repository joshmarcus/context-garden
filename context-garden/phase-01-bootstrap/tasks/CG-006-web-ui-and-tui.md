---
id: CG-006
title: Web UI and TUI
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-004]
priority: 1
estimate: M
reading:
  - context-garden/phase-01-bootstrap/specs/scheduler.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

A local FastAPI + HTMX board with task detail, graph and runs, and a Textual TUI, both thin over the store and scheduler.

## Acceptance criteria

- [x] `garden serve` runs the scheduler loop in-process and shows live state.
- [x] Approve / dispatch / cancel / retry from both UIs.
- [x] Cost and token totals visible.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
