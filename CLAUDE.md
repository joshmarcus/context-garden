# context-garden

A Python CLI (`garden`) plus web UI and TUI that drives agent development from a tree of
context files. This repo is also its own first product (`context-garden/`).

## Working here

- Install: `uv venv && uv pip install -e ".[dev]"`
- Tests: `.venv/bin/pytest -q` (uses `tests/fake_claude.py` instead of the real `claude`)
- Lint: `.venv/bin/ruff check src tests`
- Try it: `.venv/bin/garden status`, `garden graph`, `garden brief CG-008 --stats`, `garden inbox`, `garden prs`, `garden digest`, `garden serve`

## Design docs

`docs/design.md` (ideas, vocabulary, architecture, the loop) and `docs/roadmap.md`. Per-feature
specs under `context-garden/phase-01-bootstrap/specs/`.

## Where things are

- `src/garden/` the package; read `context-garden/product.md` for a module map
- `principles/` cross-cutting principles; `00-index.md` is inlined into every agent brief
- `context-garden/<phase>/` goals, specs and tasks for the tool itself
- `.claude/skills/` `garden-take`, `garden-plan`, `garden-review` for interactive sessions
- `personas/` reviewer personas for `garden persona-review`

## Rules

- Task files under `**/tasks/` are owned by the scheduler; don't hand-edit status fields
  (use `garden approve`, `garden set-status`, or the UIs).
- `model`, `store`, `graph`, `brief` must stay free of network calls.
- Keep the CLI, web and TUI thin; logic goes in `scheduler`, `graph`, `brief`, `store`.
