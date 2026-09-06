# context-garden

context-garden is a repository of context files that drives detached agent workers through a token-free scheduler. You write principles, a product overview, phase goals and specs as markdown. A planner turns a phase into task files; the scheduler runs one worker per task in its own git worktree, pushes the branch, opens the pull request, has it reviewed, and unblocks the next task once the PR merges: by you on GitHub, or by its own merge queue when you turn that on. A local web UI, a terminal UI and the `garden` CLI show the same loop and offer the same actions. The garden that drives this tool's own development lives at [joshmarcus/garden](https://github.com/joshmarcus/garden).

It is not a chat bot and it is not a CI runner. The scheduler never calls a model: waiting, polling, ordering, retrying, rebasing and bookkeeping are plain Python, and tokens are spent only on bounded planning, worker, review, persona, trial and retrospective runs.

## How the loop works

1. **Context files** describe a product and a phase: `principles/`, `<product>/product.md`, `<phase>/goals.md`, `specs/` and `docs/`.
2. **The planner** turns a phase into task files, each with acceptance criteria, a reading list and a difficulty tier that picks the model.
3. **Approve** moves a draft task to ready. Approve refuses placeholder acceptance criteria and reading-list paths that name no file.
4. **Workers** run detached, one per task, in a worktree of the product repo, with the brief on stdin and a scrubbed environment. They commit and never push.
5. **Checks** (tests, lint) run token-free in the worktree before any PR exists; a failure becomes a revise round.
6. **The PR** opens as a draft. The **automated review** posts a verdict against the acceptance criteria; a change request becomes a bounded revise run, and the Inbox asks you to triage the draft.
7. **The merge** is yours by default: once the review approves, the Inbox shows the PR with its verdict and CI, you merge it on GitHub, and the next poll marks the task done. A PR that falls into conflict with its base is rebased mechanically either way; an agent is used only on a real conflict, and an unchanged diff keeps its verdict. With `github.automerge: true` the **merge queue** does the merging: it rebases each approved easy or medium PR once, right before it merges, and a hard-tier PR needs two approving rounds and a scratch-merge check.
8. **The retro** closes the phase: it reconciles the friction, runs the personas, records a verdict and drafts the next phase's goals and features.

[docs/design.md](docs/design.md) is the idea and the vocabulary, [docs/architecture.md](docs/architecture.md) is how the pieces fit, [docs/worker-protocol.md](docs/worker-protocol.md) is the scheduler-to-worker contract and [docs/roadmap.md](docs/roadmap.md) is what is next.

![Inbox](docs/screenshots/inbox-light.png)

## Getting started

You need Python 3.11+, git, a GitHub login (`gh auth login`, or `GITHUB_TOKEN`) and at least one harness CLI: Claude Code (`claude`) or Codex (`codex`, see [docs/codex.md](docs/codex.md)). On Windows run everything in WSL: the local runner needs a POSIX shell.

Install from a checkout with `uv`, or with `pip`:

```bash
git clone https://github.com/joshmarcus/context-garden && cd context-garden
uv venv && uv pip install -e ".[dev]"       # or: python -m pip install -e ".[dev]"
.venv/bin/garden --help
```

Create a garden. `garden init` writes `garden.yaml`, `principles/00-index.md` (the digest every brief carries), `AGENTS.md`, a `.gitignore` and the four Claude Code skills under `.claude/skills/`. Every later command runs from inside the garden:

```bash
garden init my-garden && cd my-garden
git init
garden new-product widget --repo ../widget --base-branch main   # a local path or a git URL
garden new-phase widget phase-01
garden doctor
```

`garden doctor` checks the config files it loaded, the GitHub login, each harness's login through the exact environment a worker gets, the product repos and the task graph. Fix what it reports before going on.

Now write the context: `principles/00-index.md`, `widget/product.md` (what the product is, how to run and test it), `widget/phase-01/goals.md` and one or more specs under `widget/phase-01/specs/`. Then set the shared configuration in `garden.yaml`:

```yaml
name: my-garden
runner: local                      # local | ssh | manual
harness: claude                    # claude | codex | a name under harnesses:
max_parallel: 5                    # detached runs at once (work, revise, review)
max_attempts: 2
max_revisions: 3
review:
  enabled: true
  max_rounds: 2
  personas: []                     # persona reviews on every new PR round, e.g. [security]
budgets:
  widget/phase-01: 50.0            # USD cap; dispatch pauses when it is reached
github:
  draft_pr: true                   # your triage marks a PR ready for review
  automerge: false                 # false: you merge each approved PR on GitHub; true: the merge queue merges it
harnesses:
  claude: {models: {easy: haiku, medium: sonnet, hard: opus}}
  codex:  {models: {easy: gpt-5.6-luna, medium: gpt-5.6-terra, hard: gpt-5.6-sol}}
models:
  medium:                         # a pool shares work between available accounts
    - {harness: claude, model: claude-sonnet-5, weight: 2}
    - {harness: codex, model: gpt-5.6-terra, weight: 1}
dispatch: {spread: quota_aware}   # round_robin | weighted | quota_aware
checks:
  pre_pr: [{name: tests, command: "pytest -q -x"}]
products:
  widget:
    repo: ../widget
    base_branch: main
    id_prefix: WID
    setup: {command: "uv venv && uv pip install -e .", test: "pytest -q", lint: "ruff check ."}
```

Difficulty tiers route each task to a model, so cost follows difficulty. A top-level tier can instead be a pool of harness/model members: `round_robin` alternates, `weighted` repeats members by weight, and the default `quota_aware` behavior uses those weights while skipping a harness paused after a quota error. A task's `harness:` or `model:` remains a pin. `review.pool` accepts the same member list for alternating review accounts, and `garden trial -c tier:medium` expands a tier pool into contenders. `garden.<GARDEN_ENV>.yaml` and a gitignored `garden.local.yaml` layer on top for a work or per-machine setting; [examples/garden.work.yaml](examples/garden.work.yaml) shows ssh workers, a Jenkins log analyser and a product whose dependencies and tests are not Python.

Plan the phase into drafts, read the plan, approve it, then start the loop:

```bash
garden plan widget/phase-01 --draft     # runs a kickoff first when the phase has none; --no-kickoff skips it
garden trellis                          # dependencies and stacks
garden validate                         # graph and reading lists
garden approve --all widget/phase-01    # draft -> ready; nothing dispatches before this
garden serve                            # web UI at http://127.0.0.1:8765 plus the scheduler loop
```

Within a tick the first ready task is running in `.garden/worktrees/WID-001`. When the worker finishes, the scheduler commits leftovers, runs the pre-PR checks, pushes, opens a draft PR and starts the automated review. The Inbox then shows your first card: triage the PR ready for review or send it back with a note. Once the review approves, the card shows the verdict and CI with a link to the PR; merge it on GitHub (`gh pr merge`), and the next poll marks the task done and dispatches whatever it unblocked. Set `github.automerge: true` to let the garden merge approved PRs itself. From another terminal, `garden status` is the overview, `garden inbox` the same cards as text, and `garden observe` the operator's feed. `garden tick` runs one pass and `garden watch` loops without the web server.

## Operating a garden

The web app is the operator's desk. **Inbox** is the home page: every card that needs a person, with its action inline. **Board** shows tasks by state, **Trellis** the dependency and stacking structure, and the phase pages carry goals, burn-up, tracked PRs, reports and the retro. A task page shows the brief, the timeline, every run, the review verdict and every action; a run page keeps the brief, transcript, result and cost. **Timeline**, **Trials** and **Runs** are the event log, the model leaderboard and every run. **Costs** slices spend by activity, difficulty, model, harness, phase, task or operator session. **Config** shows the effective configuration, the live overrides (pause, `max_parallel`, the operating and observe profiles) and the few keys that need a restart. **Herbarium** holds the closed phases. `garden tui` is the same Inbox and task list in the terminal.

Every card has one action, and every action is also a CLI command. Act through them, never by editing a task file:

- A discovered draft: approve or cancel it (`garden approve`, `garden cancel`).
- A worker's question: answer it and the same session resumes (`garden answer`).
- A worker's `wont_do` or `no_change` call: accept or reject it with a note (`garden accept`, `garden reject`).
- A draft PR: triage it ready for review, or send it back with feedback (`garden triage --ready`, `--changes`).
- A PR in review: merge it on GitHub once the verdict and CI are green; with `automerge` on, the same card says why the queue holds it, and the Inbox's merge-queue panel shows the head in flight.
- A stopped task (review cap, revision cap, failed): retry it or start one more review (`garden retry`, `garden review`).
- A decision card, from a worker's duplicate or cancel finding or from a kickoff question: accept, reject, answer or dismiss (`garden decide`).

`garden pause` and `garden unpause` stop and restart automatic dispatch while reap, poll and reviews go on. `garden set max_parallel 3` and `garden profile economy` (stops: `economy`, `balanced`, `fast`, or your own under `profiles:`) change the operating point live, in effect on the next tick, as does any edit to `garden.yaml`.

A phase has a lifecycle. `garden kickoff widget/phase-01` runs one planner-tier pass that files design gaps and owner questions as cards before planning. `garden freeze widget/phase-01` stops approvals and dispatch until `garden unfreeze`, so a phase can land what it has. `garden retro widget/phase-01` runs the retrospective as one process: the missing persona reviews, the friction reconciliation, the verdict and the next phase's goals and feature drafts, opened as a PR to the garden's own repo (a product with `self: true`). A `close` verdict closes the phase into the Herbarium; a `reopen` verdict files the blocking items as tasks and waits for `garden retro-decide`. `garden close-phase` and `garden reopen-phase` do the same by hand.

`garden trial WID-003 -c claude:sonnet -c codex:gpt-5.6-terra` runs one task once per contender, has a comparison run score the PRs, keeps the winner and records the scores; `garden trials` is the leaderboard. `garden budget widget/phase-01 50` caps a phase's spend live; dispatch pauses at the cap and resumes when it is raised. `garden observe` prints a status line, the cards, stuck runs, tracebacks and a digest; `--profile quiet` (the default) reports questions, stops and failures, `watch` adds the loop's own decisions, and `debug` streams every transition. `--follow` keeps it open and `--json` emits one object per pass for an agent.

## Running it as a service

For a garden that runs overnight, run `garden serve` as a systemd user service. Workers get a private HOME, so the harnesses' saved logins must be named through their config directories; `KillMode=process` lets detached workers outlive a restart of the server:

```ini
# ~/.config/systemd/user/garden-serve.service
[Unit]
Description=context-garden scheduler and web UI

[Service]
WorkingDirectory=%h/gardens/my-garden
Environment=CLAUDE_CONFIG_DIR=%h/.claude
Environment=CODEX_HOME=%h/.codex
ExecStart=%h/gardens/my-garden/.venv/bin/garden serve
Restart=on-failure
KillMode=process

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now garden-serve.service
journalctl --user -u garden-serve.service -f        # the serve log and any traceback
```

Restart only right after a tick has saved `.garden/state.json` (its mtime is the tick clock), so a verdict the old process just reaped is not lost. Never start a second `garden serve` beside it, and never stop it by killing its process tree: the workers are in that tree.

```bash
systemctl --user restart garden-serve.service
systemctl --user is-active garden-serve.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
```

On WSL, enable systemd in `/etc/wsl.conf` and turn on lingering (`loginctl enable-linger $USER`) so the user service comes back when the instance restarts. If WSL itself was stopped mid-run, the killed workers leave uncommitted edits in their worktrees and the next dispatch there fails on the fast-forward; run `git stash push -u` in that worktree, then `garden retry` the task.

## Restarting an operator agent session

An operator session is an interactive Claude Code session that runs the `garden-operate` skill, installed by `garden init` at `.claude/skills/garden-operate/SKILL.md`. It watches the loop, clears cards through the product's own actions and files every reason the loop needed it as a task. The session ends often: a context compaction, a machine restart, a new day. Nothing about the garden lives in that session, so ending it loses nothing: task files are the state, `.garden/state.json` is the scheduler's side-store, `.garden/runs/` holds every brief and result, `.garden/events.jsonl` is the history, and the workers are detached processes that finish on their own.

When a session starts or resumes, in this order:

1. Read `.claude/skills/garden-operate/SKILL.md`: where the truth is, what each stall looks like and which action clears it.
2. `garden observe`, or `garden status` followed by `garden inbox`: the pulse, the cards and anything stuck.
3. `git log` of the garden repo, for commits you did not make; then the open phase's `goals.md` and `docs/`.
4. Open the raw event log or a run directory only to answer a specific question.

Never edit a task's `status:` by hand, edit `state.json` while a tick may be writing it, answer a worker with an instruction that sends it outside its worktree, or stop the service with a process-tree kill. Before pressing an action on a task, confirm its PR is still open.

The operator is the most expensive seat when it is an agent, because every turn re-reads its whole context. Observe on the `quiet` profile unless you are chasing something. Compact at boundaries (a phase closing, a retro merging, the start of a long wait), after writing what the next session needs into the phase's docs. Record the session's spend with `garden operator-spend record` (a heartbeat read from the session transcript) and `garden operator-spend record --compacted --session <id>` at each compaction, so the operator's share appears beside the workers'.

## Costs

Every run records its harness, model, tokens and cost in its run directory, and the tier map in `harnesses.<name>.models` decides which model each difficulty gets (an explicit `model:` on a task wins). `garden usage` rolls cost up per task or phase, `garden metrics` gives revise rounds, first-pass approval and cost per tier, and `garden costs --by model` slices spend over time the way the Costs page does. Operator sessions are recorded in the garden's `docs/operator-spend.jsonl` and appear as the `operator` activity in `garden costs` and on the Costs page, so the retro can report the operator's share of a phase rather than guess it.

## Skills and contributing

`garden init` installs four Claude Code skills under `.claude/skills/`, so an interactive session can take any seat in the loop:

- `garden-take`: claim a task through the manual runner, do the work, hand the result back so the garden opens the PR.
- `garden-plan`: plan a phase from the session instead of a headless planner call.
- `garden-review`: review a task's PR against its brief and post the verdict.
- `garden-operate`: watch and troubleshoot a running loop.

`garden validate` checks the graph and the reading lists before anything is dispatched. To work on context-garden itself:

```bash
uv venv && uv pip install -e ".[dev]"
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
```

The tests stand in fake harnesses for `claude` and `codex`, so they spend no tokens. MIT licensed; see [LICENSE](LICENSE).
