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

Support `output_format: stream-json` for the claude harness and tail the event log on the task page while a worker runs.

## Context

The command line and output parsing for a harness live in `src/garden/harness.py` (`Harness.command`, `Harness.parse`); the local runner in `src/garden/runner/local.py` only starts the process and writes `stdout.json`. Add an `output_format` key to the claude harness config, and teach `parse` to read a stream of JSON lines as well as the single result object. The web app is `src/garden/web/app.py` with Jinja templates; live regions are elements with a `data-poll` attribute that fetch a partial every few seconds (no HTMX, no CDN), so add a partial that tails the run's `stdout.json`. Keep the default output format `json` so existing tests pass; add tests for the stream path using `tests/fake_claude.py` (extend it with a `--stream` mode).

## Acceptance criteria

- [ ] `collect()` handles both formats.
- [ ] Task page shows the last 50 events for a running task, refreshing every 3s through `data-poll`.
- [ ] TUI detail pane shows the same tail.

## Out of scope

- Interactive steering of a running worker.
