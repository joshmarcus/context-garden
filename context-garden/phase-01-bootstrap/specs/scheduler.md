# Scheduler

`garden tick` is one pass of a deterministic state machine. `garden watch` and the web
server loop it on `tick_interval`. No LLM is called by the scheduler.

## One tick

1. **Reap.** For each `running` task, look at its latest run record. If the worker process
   has finished (`exit_code` file present, or pid gone), collect its output:
   - parse the trailing `GARDEN_RESULT: {...}` line from the final message;
   - `status: blocked` -> task `failed` with the worker's question in the log;
   - no result / non-zero exit -> retry (back to `ready`) until `max_attempts`, then `failed`;
   - otherwise commit any leftover changes, require >= 1 commit ahead of base, push the
     branch, open the PR (or comment on the existing one for revise runs) -> `in_review`.
   A run older than `timeout_minutes` is killed and treated as failed. For `ssh` runs the
   worker pushed the branch itself; the scheduler fetches it and materialises a local
   worktree instead of pushing. After the PR is opened or updated, a review run is
   dispatched (see review-pass.md); when it finishes it is reaped here too, its verdict
   posted on the PR, and `request_changes` routes into step 2's revise path.
2. **Poll.** For each `in_review` task with a PR: merged -> `done` (worktree removed);
   closed -> `failed`; otherwise, if the PR's `updated_at` changed, fetch reviews and
   comments newer than `last_dispatched_at` (ignoring the garden's own login and bots) and
   the CI rollup. New feedback or a red CI -> `changes_requested` with the feedback stored
   in `.garden/state.json` for the revise brief.
3. **Dispatch.** Fill free slots (`max_parallel` minus running detached workers, review
   runs included): revise runs for `changes_requested` tasks first, then `ready` tasks
   whose dependencies are all `done`, ordered by priority then id. The model is chosen
   from the task's difficulty via the harness's tier map. Tasks whose runner is `manual`
   are skipped; humans take those with `garden take`.

Every PR the garden opened (or attached with `garden pr`) is tracked until merged or
closed: `garden prs [product/phase]` and the phase page list state, review decision, CI
rollup with failed check names, revision and review counts, and the last poll time.

## Run records

`.garden/runs/<task>/<run-id>/` holds `run.json`, `brief.md`, `stdout.json`, `stderr.log`,
`final.md`, `exit_code`. Cost and token usage are copied from the `claude -p` JSON
result into `run.json`, so `garden runs` and the web UI can total them.

## Worktrees

`.garden/worktrees/<task>` is a git worktree of the product repo on the task's branch,
created from `origin/<base>` (or the local base branch when there is no remote). Remote
product repos are cloned once under `.garden/repos/`.
