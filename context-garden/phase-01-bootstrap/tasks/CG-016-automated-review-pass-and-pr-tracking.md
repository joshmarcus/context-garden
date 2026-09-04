---
id: CG-016
title: "Automated review pass and PR tracking"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-014]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/review-pass.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Review every PR round automatically (criteria, correctness, scope, PR description quality) and track every PR in a phase.

## Acceptance criteria

- [x] Review run per PR round, bounded by `review.max_rounds`; verdict posted on the PR; request_changes feeds the revise loop.
- [x] Revise runs replace the PR description from `pr_body`.
- [x] `garden prs` and the phase page show review decision, CI with failed check names, revisions, last poll.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
