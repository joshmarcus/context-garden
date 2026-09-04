---
id: CG-011
title: Automated review pass before human review
status: done
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 2
estimate: M
reading:
  - context-garden/phase-02-friction/specs/review-pass.md
  - context-garden/phase-01-bootstrap/specs/scheduler.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Optional one-call review of each new PR round, posting findings as a PR review and routing blocking findings through the revise path.

## Context

Add a `review.py` module with `review_prompt(brief, diff)` and `run_review` mirroring `planner.py`. The scheduler calls it in `_open_or_update_pr` when `review.enabled`. The diff comes from `git diff base...HEAD` in the worktree. Findings are posted with `GitHub.comment` (extend with a review API if simple).

## Acceptance criteria

- [ ] Off by default; when on, exactly one review call per PR round, bounded by `review.max_rounds`.
- [ ] Blocking findings -> `changes_requested` with the findings as pending feedback.
- [ ] Tests with the fake claude binary returning canned findings.

## Out of scope

- Line-anchored review comments (a single summary comment is fine).

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap phase (see phase-01-bootstrap/specs/review-pass.md); the stream-json dependency was not needed
