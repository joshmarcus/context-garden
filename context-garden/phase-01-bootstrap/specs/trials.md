# Model trials

`garden trial ID -c claude:sonnet -c claude:opus [-c codex:gpt-5]` runs one task with
several contenders (harness:model). Each contender gets its own branch
(`<task-branch>-trial-<label>`), worktree and run (mode `trial`); the task shows `running`
with the trial in `state.json`.

When every contender finishes, each successful one is pushed and gets a PR titled
`[trial <label>] ...`. With two or more PRs, one **comparison run** (a review-tier model)
receives the task brief, every PR description and diff, and the worktree paths, and ends
with `GARDEN_COMPARE: {"winner", "rationale", "ranking": [{"label", "score" 0-10, "summary"}]}`.
The winner's branch and PR become the task's; the losers' PRs are closed with the ranking
posted on every PR; loser worktrees are removed; the task moves to `in_review` (the
comparison stands in for the automated review). A single surviving contender wins by
default; none means `failed`.

Each trial is appended to `.garden/trials.jsonl` with per-contender status, score, cost
and PR. `garden trials` (and the Trials page) shows the leaderboard: trials, wins, win
rate, average score and average cost per contender. Use it to decide the tier-to-model map.
