# context-garden

A tool for driving agent development from a repository of context files. Humans write
principles, product overviews, phase goals and specs. A planner turns a phase into task
files. A token-free scheduler dispatches headless agent workers per task, pushes their
branches, opens PRs, and re-dispatches when reviewers leave feedback. A local web UI and
a TUI show the board and the dependency graph.

Users: a single developer (or small team) who wants to specify work and review PRs, and
have the rest of the loop automated without an LLM sitting in the scheduler seat.

## Repo

This repository is both the tool and its own first product. Python 3.11+, packaged with
`pyproject.toml` (hatchling), managed with `uv`.

- Install: `uv venv && uv pip install -e ".[dev]"`
- Tests: `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check src tests`
- CLI: `.venv/bin/garden --help`

Layout:

- `src/garden/model.py` task frontmatter model and statuses
- `src/garden/store.py` discovery of products/phases/tasks on disk
- `src/garden/graph.py` dependency graph, ready set, mermaid export
- `src/garden/brief.py` builds the worker brief; `GARDEN_RESULT` parsing
- `src/garden/scheduler.py` the tick state machine (reap / poll / dispatch)
- `src/garden/harness.py` harness definitions (claude, codex, custom) and output parsing
- `src/garden/runner/` runner backends (`local`, `ssh`, `manual`)
- `src/garden/review.py` automated review brief and verdict parsing
- `src/garden/events.py` append-only event log, digest and metrics
- `src/garden/gitops.py`, `src/garden/github.py` git worktrees and PRs
- `src/garden/planner.py` planning prompt and JSON import
- `src/garden/web/` FastAPI + HTMX web UI; `src/garden/tui/` Textual TUI
- `tests/` pytest; `tests/fake_claude.py`, `fake_codex.py`, `fake_ssh.py` stand in for the real binaries

## Conventions

- Type hints everywhere; `from __future__ import annotations`.
- No network calls in `model`, `store`, `graph`, `brief`; those must stay testable offline.
- Task files are the source of truth; `.garden/` holds only run records and PR bookkeeping.
- Keep the CLI, web and TUI thin: all logic lives in `scheduler`, `graph`, `brief`, `store`.
