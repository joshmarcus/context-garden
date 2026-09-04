---
id: CG-014
title: "Harness abstraction and difficulty-based model selection"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-004]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/harness.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Make the worker harness pluggable (claude, codex, custom command) and pick the model from the task's difficulty tier.

## Acceptance criteria

- [x] `harnesses:` config with built-in claude and codex definitions; `garden doctor` checks binaries.
- [x] `difficulty` on tasks, emitted by the planner; per-harness tier -> model map; explicit `model` override.
- [x] Runs record harness, model, usage and cost.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
