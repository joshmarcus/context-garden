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

Built in helpers for writing analysers: `garden.checks:local_command_check` runs a
command of yours (e.g. a script that fetches the failing job log from your CI system)
and turns its output into a result using `interesting_lines` (keeps error-looking lines)
and `classify_log` (`flaky` when `flaky_patterns` match), returning `retry_command` when
configured. The garden does not depend on GitHub Actions or any particular CI system.
`garden check ID [--stage ci]` runs the checks by hand.
