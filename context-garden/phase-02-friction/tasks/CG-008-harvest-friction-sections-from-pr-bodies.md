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

The last work or revise run of a task keeps the PR body it wrote in `run.json` (`result.pr_body`, see `RunStore`); use that first so the command works offline, and fall back to `GitHub.get_pr`, whose `PRInfo.body` already carries the live body. The planner already includes `docs/*.md`, so the file it writes is read on the next `garden plan`.

## Acceptance criteria

- [ ] Command writes a grouped markdown file with task id, title, PR link and the friction text.
- [ ] Running it twice is idempotent.
- [ ] Unit test with a fake GitHub object.
- [ ] Web UI task detail shows the friction section if present.

## Out of scope

- Summarising the friction with a model.
