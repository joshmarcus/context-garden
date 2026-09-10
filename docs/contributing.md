# Contributing

context-garden is a Python 3.11+ CLI, FastAPI web UI, and Textual TUI. Keep interface layers
thin: reusable behavior belongs in package modules, and lifecycle transitions belong in the
scheduler. `model`, `store`, `graph`, and `brief` remain network-free.

## Development setup

Work in a dedicated Git checkout or worktree and preserve unrelated local changes. Linux
and macOS are supported; use WSL on Windows. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
.venv/bin/python -m pytest tests/test_cli.py -q
.venv/bin/ruff check src tests scripts
```

Tests use fake harnesses and repositories and should not spend model tokens. Never point a
development test at a live garden unless the test explicitly requires it. Stress and load
experiments are opt-in and are not part of ordinary pytest or routine CI. Use the selection
table and timeout guidance in [focused test suites](test-suites.md).

## Find the right layer

- `src/garden/model.py`, `store.py`, `graph.py`, and `brief.py` hold offline domain logic.
- `src/garden/scheduler/` owns state transitions, dispatch, checks, review, and recovery.
- `src/garden/runner/` and `harness.py` define execution transports and agent CLIs.
- `src/garden/cli/`, `web/`, and `tui/` render state and forward actions.
- `tests/scheduler/` mirrors scheduler concerns; other `tests/test_*.py` files follow their
  package areas.

Read [architecture](architecture.md) for the module map and state machine, [design](design.md)
for product vocabulary, and [worker protocol](worker-protocol.md) before changing dispatch,
result parsing, runners, checks, or recovery.

The principal extension points are custom harness configuration, runner implementations,
check commands or Python analysers, and persona Markdown. Match an existing implementation
and its focused tests before adding a new mechanism. Keep network calls out of the offline
core and avoid adding dependencies when the standard library or an existing package works.

## Make and verify a change

Start with the smallest test file that exercises the behavior, then add adjacent suites if
the change crosses a boundary. Run lint after tests. The full project gate is
`python3 scripts/check_ci.py`; use it only in the environment and branch workflow described
by [worker CI](worker-ci.md), because it may publish the branch and wait for remote CI.

For rendered UI changes, inspect the affected pages at representative wide and narrow
viewports and in supported themes. For documentation, check links, copy commands from the
reader's working directory, and distinguish current behavior from roadmap material.

Commit coherent changes with descriptive messages. Automated workers must commit in their
assigned worktree and emit the required `GARDEN_RESULT`; the scheduler, not the worker,
normally pushes and opens the PR.

## Maintain the documentation

Update the canonical page instead of creating parallel instructions:

- product positioning and tour: [README](../README.md);
- first successful run: [getting started](getting-started.md);
- commands and scripts: [CLI guide](cli.md);
- operation and recovery: [operations](operations.md);
- implementation contracts: [architecture](architecture.md) and
  [worker protocol](worker-protocol.md).

Use synthetic names, hosts, paths, and outputs. Do not publish credentials, private host
identities, customer names, or raw operational transcripts. Verify command help against the
current checkout and link feature-specific references instead of copying their internals.
