---
id: CG-009
title: Stream-json runner output and live log in the web UI
status: draft
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 2
estimate: M
difficulty: medium
reading:
  - context-garden/phase-02-friction/specs/live-output.md
  - context-garden/phase-01-bootstrap/specs/scheduler.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Support `claude.output_format: stream-json` in the claude-local runner and tail the event log on the task page.

## Context

`ClaudeLocalRunner.command` and `collect` live in `src/garden/runner/claude_local.py`. The web app is `src/garden/web/app.py` with Jinja templates; HTMX is already loaded. Keep the default output format `json` so existing tests pass; add tests for the stream path using `tests/fake_claude.py` (extend it with a `--stream` flag).

## Acceptance criteria

- [ ] `collect()` handles both formats.
- [ ] Task page shows the last 50 events for a running task, refreshing every 3s.
- [ ] TUI detail pane shows the same tail.

## Out of scope

- Interactive steering of a running worker.
