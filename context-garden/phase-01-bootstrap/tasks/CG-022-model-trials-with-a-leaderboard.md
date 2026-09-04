---
id: CG-022
title: "Model trials with a leaderboard"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-016]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/trials.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Run one task with several harness/model contenders, compare the PRs, keep the winner and record relative scores.

## Acceptance criteria

- [x] `garden trial ID -c a:b -c c:d`; per-contender branch, worktree, run and PR; one comparison run; losers closed with the ranking posted.
- [x] `.garden/trials.jsonl` and `garden trials` leaderboard (wins, avg score, avg cost).

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
