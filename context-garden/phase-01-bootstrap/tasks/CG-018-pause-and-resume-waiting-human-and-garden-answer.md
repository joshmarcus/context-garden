---
id: CG-018
title: "Pause and resume: waiting_human and garden answer"
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

Workers can ask a question and resume in the same session after the human answers.

## Acceptance criteria

- [x] `needs_input` result -> `waiting_human` (no slot held); `garden answer` resumes via `--resume` or a fresh run with the Q&A.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
