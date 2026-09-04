# Roadmap

The garden's own phases are the plan of record: `context-garden/phase-*/goals.md` and
their task files. This page is the human summary.

## Shipped (phase-01-bootstrap)

Layout and task format; dependency trellis with stacking; briefs with size budgets;
deterministic scheduler (reap / poll / dispatch); local, ssh and manual runners; claude and
codex harnesses with difficulty-based model choice; planner; automated review with PR
description standards; persona reviews (phase and PR); model trials with a leaderboard;
token-free pre-PR and CI checks with flaky reruns; pause/resume on questions; discovered
work; stall detection and budgets; event log, digest and metrics; web UI and TUI; skills
for interactive sessions.

## Next (phase-02-friction)

Run the tool on itself and fix what hurts: harvest friction from PR bodies into the phase
docs, live worker output in the web UI, a notification hook, a brief cost report,
replanning after failures. Every task in that phase is meant to ship through
`garden tick`, not by hand.

## Later, in rough order

- **Event-driven wakeups.** GitHub webhooks (or `gh` polling with ETags) to replace the
  fixed tick interval when something changes on a PR.
- **Context compaction.** When a phase closes, fold what still matters into the product
  overview and archive the rest, so the fixed brief cost stays flat as the garden grows.
- **Line-anchored review comments** from the automated review and personas.
- **Trial calibration.** Use trial scores and `garden metrics` to suggest tier-to-model
  map changes automatically ("easy tasks lose nothing on the cheap model").
- **Remote runner for Claude Code on the web** once a public session API exists.
- **Multi-product phases** that span repos (a task touching two products).

## Explicitly not planned

Hosted service, accounts, automatic merging, or an LLM acting as the scheduler.
