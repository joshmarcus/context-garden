# The agent loop, and where tokens go

The loop is: human writes goals and specs -> planner emits tasks -> human approves ->
scheduler dispatches workers -> workers open PRs -> human reviews -> scheduler dispatches
revisions -> PR merges -> dependents unblock.

Only three steps spend tokens: planning, working, revising. Everything else (waiting for
workers, polling PRs, ordering the queue) is deterministic Python with no model in the loop.
An agent session used as a scheduler burns tokens *while waiting*; a Python process does not.

## Cost controls

- **Brief budget.** `garden brief <id> --stats` shows the exact prompt and its size. Keep
  the fixed sections small, and keep reading lists to what the worker needs.
- **One PR per task.** Workers do not push or open PRs; the runner does, deterministically.
- **Bounded retries.** `max_attempts` and `max_revisions` cap runaway loops.
- **Revisions carry only the delta.** A revise brief adds the review comments since the
  last dispatch, not the whole thread.
- **Planning is one call.** The planner sees goals, specs and the existing task list once
  and emits JSON. Humans edit the resulting files by hand.
