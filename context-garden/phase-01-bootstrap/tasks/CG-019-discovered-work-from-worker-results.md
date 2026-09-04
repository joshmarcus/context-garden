---
id: CG-019
title: "Discovered work from worker results"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-017]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/coordination.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Out-of-scope work a worker notices becomes task files with provenance.

## Acceptance criteria

- [x] `discovered` in `GARDEN_RESULT` -> tasks with `discovered_from`; blocking ones ready; dashed graph edges; `ls --discovered`.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
