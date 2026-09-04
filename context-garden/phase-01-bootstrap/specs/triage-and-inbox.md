# Draft PRs, triage, and the inbox

## Draft PRs and triage

With `github.draft_pr: true` (default) every PR the garden opens is a draft, and the task
sits in `awaiting_triage`. That is a tracked human step: the person's first look. The
automated review and any configured personas run while the task waits, so the human sees
their verdicts before deciding. Two outcomes:

- `garden triage ID --ready` (or the inbox button, or `y` in the TUI, or "Ready for
  review" on GitHub, which the poll detects): the PR is marked ready and the task moves
  to `in_review`.
- `garden triage ID --changes "..."`: the note becomes pending feedback and a revise run
  follows; the PR stays a draft and returns to `awaiting_triage` afterwards.

Comments left on a draft PR are still picked up as feedback. Dependents can stack on a
draft PR. Converting a ready PR back to draft on GitHub returns the task to triage.

## The inbox

`garden inbox`, the web home page, and the TUI's Inbox tab render one list
(`garden.inbox.build_inbox`): everything that needs a person, grouped by the decision
being asked for, each with the action that resolves it.

| group | when | actions |
|---|---|---|
| question | `waiting_human` | answer (resumes the session) |
| triage | `awaiting_triage` | ready for review / send back / open PR |
| review | `in_review` | open PR on GitHub / mark done |
| attention | `needs_human` flag or `failed` | continue the loop / cancel |
| approve | `draft` tasks, including discovered ones | approve / drop |
| budget | phase over its cap | raise in garden.yaml |

## CI without GitHub Actions

Nothing in the garden depends on GitHub Actions. CI state comes from the GitHub checks
rollup on the PR head, whichever system posts it; log analysis is whatever you plug into
`checks.ci` (a script that queries your CI, or a `python:` callable using the helpers
in `garden.checks`).
