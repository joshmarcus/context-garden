---
id: CG-024
title: "Token-free pre-PR and CI checks"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-004]
priority: 1
estimate: M
difficulty: hard
reading:
  - context-garden/phase-01-bootstrap/specs/checks.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Pluggable scripts / Python callables that gate PR creation and analyse red CI without spending tokens.

## Acceptance criteria

- [x] `checks.pre_pr` failures become a revise round before the PR exists.
- [x] `checks.ci` results feed the revise brief; flaky verdicts rerun jobs once instead of revising.
- [x] Helpers for writing analysers against any CI system; `garden check ID`. (No dependency on GitHub Actions.)

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
