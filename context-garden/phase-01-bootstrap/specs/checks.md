# Token-free checks

`checks:` in `garden.yaml` declares scripts or Python callables that inspect a worktree or
a red CI run and return structured findings. No model is involved; the point is to spend
tokens only on the fix, never on reading logs.

- `checks.pre_pr`: run in the worktree after a work/revise/resume run pushes and before
  the PR is opened or updated. Any failure becomes pending feedback (the check's name,
  summary and the last lines of output) and a revise round; the PR is opened only once
  the checks pass. Bounded by `max_revisions` and stall detection.
- `checks.ci`: run when a PR's CI rollup turns red. Results are appended to the CI note
  in the revise brief (failing job names plus whatever the analyser extracted). A check
  may answer `flaky`; if every non-passing check says flaky and one carries a
  `retry_command`, the scheduler runs it once (`ci_reruns` cap) instead of dispatching a
  revise run.

Check definition: `{name, command}` (exit 0 = pass; JSON on stdout is used as the result)
or `{name, python: "module:function"}` called as `function(ctx, spec)`. `ctx` carries task
id, product, phase, branch, base, repo slug, PR number, head sha, failed check names and
the worktree path (as `GARDEN_*` env vars for commands). Result:
`{"status": pass|fail|flaky|error, "summary", "details", "retry_command"?}`.

Built in: `garden.checks:github_actions_failures` uses the `gh` CLI to fetch failed job
logs for the PR head, keeps only error-looking lines, matches `flaky_patterns`, and with
`rerun: true` returns a `gh run rerun --failed` command. `garden check ID [--stage ci]`
runs the checks by hand.
