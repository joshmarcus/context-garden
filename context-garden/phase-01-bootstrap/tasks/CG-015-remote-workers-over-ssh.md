---
id: CG-015
title: "Remote workers over ssh"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-014]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/remote-runner.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Run workers on remote hosts that each hold a clone of the product repo, refreshing the clone before each run.

## Acceptance criteria

- [x] `runner: ssh` with per-host repo paths and capacity; least-loaded host selection.
- [x] Remote script fetches, works in a worktree, pushes; local scheduler opens the PR.
- [x] End-to-end test with a fake ssh and a second clone.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
