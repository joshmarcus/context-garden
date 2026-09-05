# context-garden

This Python CLI, web UI and TUI drives autonomous development from context documents.
The garden driving this tool lives separately at https://github.com/joshmarcus/garden.

Read `docs/architecture.md` for the module map, `docs/design.md` for the design, and
`docs/worker-protocol.md` for the scheduler/worker protocol. Codex setup and interactive
workflow: `docs/codex.md`. For a dispatched task, use its supplied brief and reading list.

## Development

- Install: `uv venv && uv pip install -e ".[dev]"`.
- Tests: `PYTHONPATH=src .venv/bin/python -m pytest -q`.
- Lint: `.venv/bin/ruff check src tests`.
- In a worktree without a venv, use an available Python environment with the dev
  dependencies and `PYTHONPATH=src` so tests exercise this worktree's source.
- Keep model, store, graph and brief free of network calls. Keep CLI, web and TUI
  thin; put orchestration in the scheduler and reusable logic in package modules.
- Task files and status belong to the scheduler; use garden commands for transitions.
- Automated workers commit in their assigned worktree and emit the brief's result
  marker. The scheduler pushes and opens PRs. Workers must not run the controller
  (tick/watch/serve/dispatch/take/finish) or modify its checkout or `.garden` state.
- Preserve local config overlays and unrelated task changes.
