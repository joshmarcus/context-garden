---
id: CG-008
title: Harvest friction sections from PR bodies
status: draft
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 1
estimate: M
difficulty: medium
reading:
- context-garden/phase-02-friction/specs/friction-log.md
- context-garden/phase-01-bootstrap/specs/scheduler.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Add `garden friction <product>/<phase>` that collects `## Friction` sections from task PR bodies into `<phase>/docs/friction.md`.

## Context

PR bodies come from `GitHub.get_pr`; extend it (or add `pr_body`) to return the body. Use the same gh/REST split as the rest of `github.py`. The planner already includes `docs/*.md`.

## Acceptance criteria

- [ ] Command writes a grouped markdown file with task id, title, PR link and the friction text.
- [ ] Running it twice is idempotent.
- [ ] Unit test with a fake GitHub object.
- [ ] Web UI task detail shows the friction section if present.

## Out of scope

- Summarising the friction with a model.
