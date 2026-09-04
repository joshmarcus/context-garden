# context-garden

Drive agent development by tending a context garden.

You write **principles**, **product overviews**, **phase goals** and **specs** as markdown.
A planner turns a phase into **task files**. A token-free **scheduler** dispatches headless
agent workers one task at a time, pushes their branches, opens PRs, and re-dispatches when
you leave review comments. A local **web UI** and a **TUI** show the board, the dependency
graph, and where the tokens went.

The human's job is: write goals and specs, approve the plan, review PRs, merge.
Everything else is the loop.

```
 you                          garden (python, no LLM)                    claude -p (tokens)
 ───                          ──────────────────────                    ──────────────────
 write goals/specs  ──────▶   garden plan ────────────────────────────▶  planner: 1 call
 approve drafts     ──────▶   ready set from the dependency graph
                              tick: dispatch into free slots ─────────▶  worker in a worktree
                              tick: reap ─ push branch, open PR
 review the PR      ──────▶   tick: poll ─ new comments / red CI ─────▶  revise run
 merge              ──────▶   tick: poll ─ merged ─ done, unblock deps
```

Only three steps spend tokens: planning, working, revising. Waiting is a Python
process sleeping, not an agent session polling.

## Install

Python 3.11+.

```bash
git clone https://github.com/joshmarcus/context-garden && cd context-garden
uv venv && uv pip install -e ".[dev]"      # or: pip install -e ".[dev]"
.venv/bin/garden doctor                    # checks claude, gh / GITHUB_TOKEN, repos, graph
```

Requirements on the machine that runs the loop: `git`, the `claude` CLI (for the
`claude-local` runner), and either the `gh` CLI (logged in) or a `GITHUB_TOKEN`.

## Quick start

```bash
garden init my-garden && cd my-garden
garden new-product widget --repo ../widget --base-branch main   # any local path or git URL
garden new-phase widget phase-01
$EDITOR principles/00-index.md widget/product.md widget/phase-01/goals.md widget/phase-01/specs/*.md

garden plan widget/phase-01            # one model call -> draft task files
garden graph                            # look at the plan
garden approve --all widget/phase-01    # or garden approve WID-001 WID-003

garden serve                            # web UI at http://127.0.0.1:8765 + the scheduler loop
# or: garden watch                      # scheduler loop only
# or: garden tick                       # one pass, e.g. from cron
```

Then review PRs on GitHub as usual. Comments you leave become the next revise run's
brief; merging marks the task done and unblocks its dependents.

This repository is itself a garden (`garden.yaml` at the root, product
`context-garden/`). `garden status` here shows the bootstrap phase (done) and a
`phase-02-friction` phase of draft tasks ready to be planned, approved and run on the tool
itself.

## Layout

```
garden.yaml                       config (runner, parallelism, products)
principles/
  00-index.md                     the digest: inlined into every brief, keep it short
  *.md                            long-form principles, cited from reading lists
<product>/
  product.md                      what it is, how to run/test it, conventions (every brief)
  <phase>/
    goals.md                      why now, goals, non-goals, definition of done (every brief)
    specs/*.md                    designs; planner input; on reading lists
    docs/*.md                     supporting docs; planner input
    tasks/<ID>-<slug>.md          one task = one PR; frontmatter is state, body is the brief
.garden/                          gitignored: runs, worktrees, clones, state.json
.claude/skills/                   garden-take, garden-plan, garden-review for interactive sessions
```

### A task file

```markdown
---
id: WID-003
title: Add rate limiting to the ingest endpoint
status: ready                 # draft | ready | running | in_review | changes_requested | done | failed | cancelled
product: widget
phase: phase-01
depends_on: [WID-001]         # `blocked` is derived, never stored
priority: 2                   # 1 = highest
estimate: M
reading:                      # garden-relative files (or dirs) inlined into the brief
  - widget/phase-01/specs/rate-limits.md
---

## Goal
## Context
## Acceptance criteria
## Out of scope
## Log                        # appended by the scheduler
```

`branch`, `pr`, `attempts`, `last_dispatched_at` and the `## Log` section are written by
the scheduler. See `context-garden/phase-01-bootstrap/specs/task-format.md`.

### The brief a worker sees

`garden brief WID-003` prints exactly what the worker gets: operating rules, the
principles digest, the product overview, the phase goals, the task body, and the reading
list inlined. `garden brief WID-003 --stats` shows the size of each section. A typical
brief is 2-4k tokens; that is the whole point.

Workers end with one line, `GARDEN_RESULT: {"status": "done", "summary": ..., "pr_title":
..., "pr_body": ...}`. They never push; the runner does.

## Commands

| Command | What it does |
|---|---|
| `garden init [dir]`, `new-product`, `new-phase`, `new-task` | scaffold |
| `garden status` / `ls` / `show ID` / `ready` / `graph [--format mermaid]` / `validate` | read the garden |
| `garden brief ID [--stats] [--revise]` | the exact worker prompt |
| `garden plan product/phase [--dry-run] [--import plan.json] [--guidance ...]` | goals + specs -> draft tasks |
| `garden approve ID... ` / `--all product/phase` | draft -> ready |
| `garden tick [--no-dispatch]` / `watch` / `serve [--no-watch]` / `tui` | run the loop, UIs |
| `garden dispatch ID [--mode revise] [--force]` | start a worker now |
| `garden take ID [--worktree]` / `finish ID --result '{...}'` | human-driven session path |
| `garden pr ID URL` / `cancel ID` / `retry ID` / `set-status ID STATUS` | manual state changes |
| `garden runs [ID]` / `log ID` / `doctor` | run records, cost, diagnostics |

## The scheduler

One `tick` is deterministic (details in `context-garden/phase-01-bootstrap/specs/scheduler.md`):

1. **Reap** finished workers: parse the result, push the branch, open or update the PR,
   `in_review`. No result or a crash retries up to `max_attempts`; a `blocked` report or
   an empty branch fails the task with the reason in its log.
2. **Poll** PRs: merged -> `done` (worktree removed, dependents unblock); closed ->
   `failed`; new review comments from humans or a red CI -> `changes_requested`, and a
   revise run is queued with just the new feedback (bounded by `max_revisions`).
3. **Dispatch** into free slots: revise runs first, then `ready` tasks whose dependencies
   are `done`, by priority.

Run it however you like: `garden watch` in a terminal, `garden serve` (loop + web UI),
or `garden tick` from cron/launchd every minute. Ticks are idempotent and safe to overlap
with the UIs.

## Runners

- **`claude-local`** (default): `claude -p --output-format json` in
  `.garden/worktrees/<ID>`, detached from the scheduler. Cost and token usage from the
  JSON result are stored per run. Configure model, max turns, permission mode and
  timeout under `claude:` in `garden.yaml`.
- **`manual`**: a human-driven session. `garden take ID --worktree` claims the task and
  prints the brief; `garden finish ID --result '{...}'` pushes and opens the PR through
  the same code path. The `garden-take` skill in `.claude/skills/` automates this for an
  interactive Claude Code session. Set `runner: manual` on a task or product to keep the
  scheduler from auto-dispatching it.

Adding a runner: subclass `garden.runner.base.Runner` (`start`, `collect`) and register
it in `garden/runner/__init__.py`. A remote runner (Claude Code on the web) is on the
phase-02 list.

## Web UI and TUI

`garden serve` (default port 8765, localhost only): board by status with polling, task
pages with actions (approve, dispatch, cancel, retry, mark done, reset revisions), the
brief and run logs, the dependency graph as inline SVG with clickable nodes, a phase page
with goals, specs, a one-click planner, and run/cost history. No build step, no CDN.

`garden tui`: the same data in the terminal. Keys: `a` approve, `d` dispatch, `t` tick,
`x` cancel, `e` reset to ready, `b` brief size, `l` last log, `f` toggle done/cancelled,
`r` refresh, `q` quit.

## Configuration (`garden.yaml`)

```yaml
name: my-garden
runner: claude-local        # default runner
max_parallel: 2             # concurrent detached workers
max_attempts: 2             # work runs before failed
max_revisions: 3            # auto revise rounds per PR before a human must step in
tick_interval: 60           # seconds, for watch/serve
auto_dispatch: true
auto_revise: true
brief:
  inline_max_chars: 24000   # bigger reading files are referenced, not inlined
  total_max_chars: 120000
claude:
  bin: claude
  model: ""                 # CLI default
  max_turns: 60
  permission_mode: acceptEdits   # or bypass (--dangerously-skip-permissions)
  allowed_tools: [Bash, Read, Edit, Write, Glob, Grep, MultiEdit]
  timeout_minutes: 90
github:
  use_gh: true              # gh CLI first, REST with GITHUB_TOKEN otherwise
  draft_pr: false
  reviewers: []
products:
  widget:
    repo: ../widget         # path relative to the garden, or a git URL (cloned under .garden/repos)
    base_branch: main
    id_prefix: WID
    runner: claude-local    # per-product override
    # github: owner/name    # only if the origin remote isn't a github.com URL
```

## Keeping tokens down

- `garden brief ID --stats` before approving a phase. The digest + product + goals are
  paid on every task; keep them dense. Reading lists should be what the worker needs.
- One PR per task, workers never push or poll. Retries and revisions are capped.
- Revise briefs carry only the comments since the last dispatch, not the whole thread.
- Planning is one call that emits JSON; humans edit files, not chat.
- `garden runs` and the Runs page show cost and input/output tokens per run, so context
  bloat shows up as a number, not a feeling.

## Prior art

Ideas borrowed from beads (git-backed issues with a ready command), spec-kit and
task-master (spec -> plan -> task DAG), Backlog.md (markdown task files with a board),
and Claude Code's headless mode. Heavier DAG engines (Temporal, Airflow, Dagger) would
work but bring a server and a mental model this doesn't need.

## Development

```bash
.venv/bin/pytest -q            # tests use tests/fake_claude.py and a local bare git remote
.venv/bin/ruff check src tests
```
