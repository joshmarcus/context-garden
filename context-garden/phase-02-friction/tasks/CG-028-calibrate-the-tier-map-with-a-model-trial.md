---
id: CG-028
title: Calibrate the tier map with a model trial
status: draft
product: context-garden
phase: phase-02-friction
depends_on:
- CG-027
priority: 2
estimate: M
difficulty: easy
reading:
- context-garden/phase-01-bootstrap/specs/trials.md
- context-garden/phase-01-bootstrap/specs/harness.md
runner: manual
created: '2026-09-04T14:02:28+00:00'
updated: '2026-09-04T14:02:28+00:00'
---

## Goal

Run CG-010 as a model trial with two contenders and record the first real cost-per-point numbers, so the tier map in `garden.yaml` rests on data rather than a guess.

## Context

Human-driven (`runner: manual`). CG-010 is an easy task with no PR, which is what a trial needs. `garden trial CG-010 -c claude:haiku -c claude:sonnet` runs it twice on separate branches, has one comparison run score both PRs, keeps the winner and closes the other (see the trials spec). `garden trials` then shows the leaderboard with cost, tokens and $ per point per contender. If the cheaper model wins, or scores close for much less, the `easy` tier stays where it is or moves down; if it loses clearly, move `easy` up a model. Record the decision either way.

Steps:

1. Confirm CG-010 is `draft` or `ready` with no PR, then `garden trial CG-010 -c claude:haiku -c claude:sonnet`.
2. Let the loop run it (`garden serve`). When the trial concludes, triage the winner's PR from the Inbox as usual and merge it.
3. `garden trials`: note score, cost and $ per point for each contender.
4. Add a "Tiers" section to `docs/friction.md` with the numbers, the decision, and what a second trial should test.
5. Change `harnesses.claude.models` in `garden.yaml` only if the numbers say so.

## Acceptance criteria

- [ ] One trial for CG-010 recorded in `.garden/trials.jsonl` with two contenders, scores and cost; the winner's PR merged and CG-010 `done`.
- [ ] `docs/friction.md` has a "Tiers" section with the numbers and the decision.
- [ ] The tier map in `garden.yaml` changed, or the reason it was not is in that section.

## Out of scope

- Trials on medium or hard tasks; one data point first.
