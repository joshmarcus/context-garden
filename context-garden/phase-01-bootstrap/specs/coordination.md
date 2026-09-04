# Coordination features

Five behaviours borrowed from graph-based agent coordination systems. All are deterministic
scheduler logic; none adds a model call except where a worker or reviewer already runs.

## 1. Stacked dependencies (`stack: true`)

A `ready` task whose one unfinished dependency has an open PR is not blocked: it is
dispatched on a worktree based on the dependency's branch, its PR targets that branch, and
`state.json` records `stack_parent` and `pr_base`. The brief gets a "Stacked branch"
section. When the parent merges, every child is retargeted to the product base and its
branch rebased onto `origin/<base>` and force-pushed (with lease). A rebase conflict
becomes pending feedback ("rebase onto main, resolve these files") and a revise run; the
runner force-pushes that run's result. A child that is running when the parent merges is
restacked when its run finishes. If a parent PR is closed unmerged, children are flagged
`needs_human`. Tasks with more than one unfinished dependency wait for all but one to merge.

## 2. Pause and resume (`waiting_human`)

A worker that needs a human decision commits what it has and reports
`status: needs_input` with a `question`. The task moves to `waiting_human` (holds no
slot); the run's session id, host and harness are kept. `garden answer ID "..."` (or the
web/TUI form) records the Q&A and dispatches a `resume` run: the same session continues
(`claude -p --resume`, `codex exec resume` when enabled) with only the answer as prompt;
harnesses that cannot resume get a fresh run whose brief carries every previous Q&A
under "Answers from the human". Resume runs do not count as attempts.

## 3. Discovered work

`GARDEN_RESULT.discovered` lists work the worker noticed but did not do:
`{title, body, difficulty, blocking}`. Each becomes a task file in the same phase with
`discovered_from: <id>`, a provenance note, the parent's reading list by default, and
status `draft`, or `ready` when `blocking` and `discovered.auto_approve_blocking`.
Duplicate titles are skipped. The PR footer lists the ids; the graph draws a dashed edge;
`garden ls --discovered` filters them. The planner's `supersedes` (replanning) reuses the
same provenance idea in the other direction.

## 4. Stall detection and budgets

Per task, `state.json` keeps `last_diff_hash` and `last_findings`. A revise run whose
diff against base is unchanged, or an automated-review round that repeats a blocking
finding from the previous round, marks the task `needs_human` (status
`changes_requested`, no further automatic revise runs) and emits a `stall` event.
`garden retry` clears the flag and lets the loop continue. Budgets: `budgets:
{"product/phase": usd}` or `products.<name>.budget_usd`; when the phase's recorded run
cost reaches the cap, dispatch for that phase pauses, one `budget` event is emitted, and
`garden status` shows spent/budget in red.

## 5. Event log, digest and metrics

`.garden/events.jsonl` is append-only: `transition`, `dispatch`, `run_finished`,
`pr_opened`, `feedback`, `review`, `waiting_human`, `answer`, `discovered`, `stacked`,
`restacked`, `stall`, `needs_human`, `budget`, `pr_closed`. `garden digest --since 24h`
summarises what needs you, PRs opened and merged, reviews, discovered work and cost.
`garden metrics [product/phase]` computes per-task lead time, runs, revise rounds,
first automated-review verdict and cost, and rolls them up per difficulty tier (first-pass
approval rate, average revisions) so the tier-to-model map can be recalibrated. The web UI
has a Timeline page and a per-task timeline; the phase page shows the tier table.
