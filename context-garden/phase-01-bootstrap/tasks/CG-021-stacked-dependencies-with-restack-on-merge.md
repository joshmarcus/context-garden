---
id: CG-021
title: "Stacked dependencies with restack on merge"
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

Start dependents on top of a dependency's open PR branch; retarget and rebase when it merges.

## Acceptance criteria

- [x] One open-PR dependency does not block; PR targets the parent branch; brief carries a stack note.
- [x] On parent merge: retarget to base, rebase, force-with-lease push; conflicts route to a revise run.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
