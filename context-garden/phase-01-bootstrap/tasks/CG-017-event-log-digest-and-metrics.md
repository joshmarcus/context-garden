---
id: CG-017
title: "Event log, digest and metrics"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-004]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/coordination.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

An append-only event log behind every transition, with a digest of what needs a human and metrics per difficulty tier.

## Acceptance criteria

- [x] `.garden/events.jsonl`; `garden digest`, `garden metrics`, `garden events`; Timeline page and per-task timeline.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
