---
id: CG-001
title: Task file format and on-disk store
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: []
priority: 1
estimate: M
reading:
  - context-garden/phase-01-bootstrap/specs/task-format.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Define the task file format (YAML frontmatter + markdown body) and the store that discovers products, phases and tasks from the directory tree.

## Acceptance criteria

- [x] `Task.parse`/`Task.render` round-trip a file, preserving unknown keys.
- [x] `Store` discovers `<product>/<phase>/tasks/*.md`, rejects duplicate ids.
- [x] Statuses match the spec; `blocked` is derived.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
