---
id: CG-002
title: Dependency graph and ready set
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

Compute the ready set, detect cycles and unknown dependencies, export mermaid.

## Acceptance criteria

- [x] `ready()` returns approved tasks whose deps are all done, best first.
- [x] Cycles and unknown deps are reported by `garden validate`.
- [x] `garden graph --format mermaid` renders in the web UI.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
