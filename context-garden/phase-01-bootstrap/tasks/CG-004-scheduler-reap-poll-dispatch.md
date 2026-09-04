---
id: CG-004
title: "Scheduler: reap, poll, dispatch"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-002, CG-003]
priority: 1
estimate: M
reading:
  - context-garden/phase-01-bootstrap/specs/scheduler.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

A deterministic tick that reaps finished workers, pushes and opens PRs, polls PRs for merges and feedback, and dispatches ready tasks into free slots.

## Acceptance criteria

- [x] Transitions covered by tests with a fake `claude` binary and a bare git remote.
- [x] Retries bounded by `max_attempts`; revisions by `max_revisions`.
- [x] Manual runner path: `garden take` / `garden finish`.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
