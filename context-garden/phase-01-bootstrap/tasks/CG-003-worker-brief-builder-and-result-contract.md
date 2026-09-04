---
id: CG-003
title: Worker brief builder and result contract
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-001]
priority: 1
estimate: M
reading:
  - context-garden/phase-01-bootstrap/specs/brief.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Build the exact prompt a worker receives from the digest, product, goals, task body and reading list, with size budgets, and parse the trailing `GARDEN_RESULT` line.

## Acceptance criteria

- [x] `garden brief <id> --stats` shows per-section sizes and token estimate.
- [x] Oversized reading files are referenced rather than inlined.
- [x] `parse_result` tolerates trailing noise.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
