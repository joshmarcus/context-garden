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

Run `notify.command` when a task needs a person: it reaches `awaiting_triage` or `waiting_human`, fails, is flagged `needs_human` (a stall or the revision cap), or its phase hits its budget.

## Context

Transitions go through `Scheduler._transition` and every other human-needed moment is an event (`events.HUMAN_KINDS`, emitted through `EventLog.emit`); hook both chokepoints, guarded by a `notify:` block in config, with a short timeout, and never raising into the tick. The Inbox and the Timeline already show these moments in the UI, so this task is only the outbound hook. Document the config in README and garden.yaml comments.

## Acceptance criteria

- [ ] Hook receives GARDEN_TASK_ID, GARDEN_STATUS, GARDEN_MESSAGE, GARDEN_PR.
- [ ] Test with a command that writes to a file.
- [ ] `garden doctor` reports whether a notify command is configured, and `garden.yaml` documents the block.

## Out of scope

- Per-channel integrations.
