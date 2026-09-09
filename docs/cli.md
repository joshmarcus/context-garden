# Use the CLI

Plan a phase, follow the workers, answer questions, and inspect results from your terminal. For installation and repository onboarding, start with the [README](../README.md#your-first-project).

The CLI, web UI, and TUI operate on the same garden, so you can switch between them as work progresses. Run these commands from your garden directory with the installed environment active. Replace `widget/phase-01` and `WID-003` with your phase and a task ID from `garden ls`.

## Plan and inspect the work

After writing the phase's goals and specs, create drafts and inspect what the workers will receive. Planning uses the configured models and may run a kickoff review if the phase has none. `--draft` keeps the tasks waiting for your approval:

```bash
garden plan widget/phase-01 --draft     # uses the configured planner
garden ls --product widget --phase phase-01
garden trellis
garden brief WID-003                   # the worker's full context
garden brief WID-003 --stats           # context size by section
garden validate
garden approve WID-003                # approve this task for dispatch
```

## Run the loop and follow progress

Run the scheduler on its own in one terminal:

```bash
garden watch
```

Use one controller per garden; `garden serve` already includes the scheduler. In another terminal, inspect the current state or follow the observation feed:

```bash
garden status
garden observe --profile quiet
garden observe --follow --profile quiet
```

The observation feed shows work in flight, decisions, and recovery needs. Ctrl+C stops following the feed; the controller and detached workers continue. `garden tui` opens the terminal Inbox and task list.

## Review results and handle decisions

Inspect a task before acting:

```bash
garden show WID-003
garden runs WID-003
garden log WID-003 -n 40               # latest run's recorded output
garden inbox                          # decisions and suggested actions
```

Choose the command that matches the task's current state:

| When you need to… | Command |
| --- | --- |
| Answer a worker's question | `garden answer WID-003 "Export only the current search results."` |
| Mark a draft PR ready for review | `garden triage WID-003 --ready` |
| Send a draft PR back with feedback | `garden triage WID-003 --changes "Add a test for exporting filtered results."` |
| Request another automated review | `garden review WID-003` |
| Retry a stopped task after fixing its cause | `garden retry WID-003` |
| Pause new dispatch, then resume it | `garden pause` / `garden unpause` |

For an interactive coding session, `garden take WID-003 --worktree` claims the task and prepares its worktree. Use `garden finish --help` for the result format and completion options when handing the work back.

## Inspect cost and outcomes

```bash
garden digest --since 24h
garden usage widget/phase-01 --by-mode
garden metrics widget/phase-01
garden costs --since 24h --by model
```

`usage` splits task spend by work, revision, review, and other run modes. `metrics` reports accepted-task outcomes, including cost, first-pass approval, revision rounds, and lead time. For scripts, `garden observe --json` emits an observation object, and costs can be exported as JSON:

```bash
garden costs --since 24h --by model --json > costs.json
```

Use `garden --help` to explore the command groups, and add `--help` to a command for its options.

## Run modes and control

| Command | What runs |
| --- | --- |
| `garden watch` | The scheduler loop in the foreground. |
| `garden serve` | The web UI and scheduler loop together. |
| `garden serve --no-watch` | The web UI without automatic scheduler ticks. |
| `garden tick` | One scheduler pass: collect results, poll PRs, and dispatch eligible work. |
| `garden observe --follow` | Repeated observations and events; this does not start the scheduler. |

`garden pause` stops new work dispatch while collection, checks, reviews, and merges continue. For installation maintenance, use the separate maintenance protocol in the [operating guide](operations.md#operate-and-recover).

Stopping the foreground controller does not stop detached workers. Use task actions and the maintenance protocol to manage work that is already running. Keep task state consistent through garden commands and the UIs.

## Model calls and automation

Planning, worker runs, automated reviews, and agent-assisted revisions use the configured harness and model. Reading a brief, graph, cost report, or recorded run does not call a model. `garden plan widget/phase-01 --dry-run` prints the planning prompt without a model call; `garden doctor` includes a small harness probe and is not an offline check.

To integrate with scripts, prefer the supported JSON outputs:

```bash
garden observe --profile quiet --json > observation.json
garden costs --since 24h --by activity --json > costs.json
```

A single observation is a JSON object. Cost output includes time buckets and grouped totals. Read the current state before choosing an action; a task may have advanced since an earlier observation. The interactive session workflow and operator handoff are covered in [the operating guide](operations.md#hand-off-the-operator-keep-the-evidence).
