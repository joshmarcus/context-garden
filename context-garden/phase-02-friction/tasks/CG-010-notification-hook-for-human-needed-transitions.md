---
id: CG-010
title: Notification hook for human-needed transitions
status: draft
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 2
estimate: M
difficulty: easy
reading:
  - context-garden/phase-02-friction/specs/notifications.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Run `notify.command` when a task needs a human (in_review, failed, revision cap).

## Context

Transitions go through `Scheduler._transition`. Add the hook there, guarded by config, with a short timeout and never raising. Document the config in README and garden.yaml comments.

## Acceptance criteria

- [ ] Hook receives GARDEN_TASK_ID, GARDEN_STATUS, GARDEN_MESSAGE, GARDEN_PR.
- [ ] Test with a command that writes to a file.
- [ ] Web UI shows an inbox strip of the last 20 human-needed events (from `.garden/state.json` or a small events log).

## Out of scope

- Per-channel integrations.
