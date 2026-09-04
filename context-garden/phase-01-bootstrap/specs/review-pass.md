# Automated review pass

After the garden opens or updates a PR, it dispatches one **review run** (a detached
worker like any other, mode `review`) when `review.enabled` and fewer than
`review.max_rounds` reviews have run for that PR.

The reviewer runs in a local worktree of the PR branch and receives: the task brief
without operating rules, the PR title and body, and the diff (inlined when under
`review.max_diff_chars`, otherwise read from git). It checks, in order: acceptance
criteria with evidence, correctness, scope, the **PR description** (broader context on
what is being accomplished and why, how it fits the phase goals, what was verified; no
scar tissue such as references to earlier review rounds, abandoned approaches, process
narration, leftover TODO/debug notes, commented-out code), and principle violations.

It ends with `GARDEN_REVIEW: {"verdict", "summary", "description_ok",
"description_feedback", "findings": [{"severity": "blocking"|"nit", "file", "line",
"summary"}]}`. The scheduler posts the result as a PR comment and, on
`request_changes`, stores the blocking findings and description feedback as pending
feedback and moves the task to `changes_requested`, so the normal revise loop addresses
them. A revise run's `pr_body` replaces the PR description. The garden's own comments are
excluded from feedback detection.

`garden review <ID>` starts a round by hand; the web task page has the same button.
