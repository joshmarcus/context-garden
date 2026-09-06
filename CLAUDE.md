# context-garden

A Python CLI (`garden`) plus web UI and TUI that drives agent development from a tree of
context files. The garden that drives this tool lives in a separate repository:
[joshmarcus/garden](https://github.com/joshmarcus/garden).

## Working here

- Install: `uv venv && uv pip install -e ".[dev]"`
- Tests: `PYTHONPATH=src .venv/bin/pytest -q` (uses `tests/fake_claude.py` instead of the real `claude`); CI for this repo runs the same in `.github/workflows/ci.yml`
- Config: `garden.yaml` + `garden.<GARDEN_ENV>.yaml` + `garden.local.yaml` (gitignored); see `examples/garden.work.yaml`
- Lint: `.venv/bin/ruff check src tests`
- Web app end to end: `.venv/bin/garden qa --scripted` drives the loop through the pages on a throwaway garden (no tokens; `tests/test_qa.py` runs the same)
- Try it: `garden init my-garden && cd my-garden && garden serve` — or clone [joshmarcus/garden](https://github.com/joshmarcus/garden) to see a live garden
- Worktrees: a task worktree has no `.venv` until its product's `setup.command` runs (the driving garden's `garden.yaml`, `setup` block); after that, always run tests as `PYTHONPATH=src .venv/bin/pytest -q` so they import this checkout's own `src/`, not wherever an editable install elsewhere happens to resolve `garden` to

## Design docs

`docs/design.md` (ideas, vocabulary, the loop), `docs/architecture.md` (how the pieces fit),
`docs/worker-protocol.md` (scheduler to worker, step by step) and `docs/roadmap.md`.

## Where things are

- `src/garden/` the package; `docs/architecture.md` has the module map
- The look is a herbarium: plants per phase and growth-stage glyphs are drawings in `src/garden/plants.py`; keep titles and copy plain

## Rules

- Task files under `**/tasks/` are owned by the scheduler; don't hand-edit status fields
  (use `garden approve`, `garden set-status`, or the UIs).
- `model`, `store`, `graph`, `brief` must stay free of network calls.
- Keep the CLI, web and TUI thin; logic goes in `scheduler`, `graph`, `brief`, `store`.

## Looking at pages

There is no browser inside WSL, but Windows Edge renders both static files and the running app, and Windows sees WSL's localhost. Before calling a UI change done, capture the pages it touches at 1280 and 390 wide, light and dark, and read the PNGs back:

```bash
EDGE="/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
"$EDGE" --headless=new --disable-gpu --hide-scrollbars --window-size=1280,2400 \
  --screenshot="C:\\Users\\joshm\\AppData\\Local\\Temp\\captures\\inbox-1280.png" "http://localhost:8765/inbox"
```

Add `--force-dark-mode` for dark, `--window-size=390,2400` for the phone width. For a static mock, copy it under `/mnt/c/...` first and pass a `file:///C:/...` URL, since Edge reads and writes Windows paths only; then copy the PNGs from the Windows temp folder into your worktree (for example `docs/design/captures/`) so they travel with the PR and stay inside the fence. Say in the PR which captures you looked at. CG-315 makes this a check the garden runs itself.
