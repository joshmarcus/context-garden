---
id: CG-029
title: 'Close the phase: friction document, what changed, and the next goals'
status: draft
product: context-garden
phase: phase-02-friction
depends_on:
- CG-008
- CG-013
- CG-028
priority: 3
estimate: M
difficulty: medium
reading:
- context-garden/phase-02-friction/docs/friction.md
- docs/roadmap.md
- principles/agent-loop.md
created: '2026-09-04T14:02:28+00:00'
updated: '2026-09-04T14:02:28+00:00'
---

## Goal

Turn the phase's friction record into a finished document and a draft of the next phase's goals, so the next planning round starts from evidence.

## Context

By the time this runs, `garden friction context-garden/phase-02-friction` (CG-008) harvests the `## Friction` sections of every PR in the phase into `docs/friction.md`, and that file also holds the "First live run" and "Tiers" sections written by hand (CG-027, CG-028). `garden metrics context-garden/phase-02-friction` gives lead time, revise rounds, first-pass approval and cost per tier; `garden digest` gives the history. Write for the person who plans the next phase: short, specific, with task ids.

## Acceptance criteria

- [ ] `docs/friction.md` is organised as: what workers reported (harvested), what the first live run showed, the tier numbers, what changed in response (with task ids), and what remains.
- [ ] A metrics table for the phase, from `garden metrics`, is in the document.
- [ ] A `phase-03` directory exists under `context-garden/` (create it with `garden new-phase`) with a draft `goals.md` drawn from the "what remains" list, and no tasks yet.
- [ ] The "Next" section of `docs/roadmap.md` points at phase 3.

## Out of scope

- Planning phase 3's tasks; that is `garden plan`, after the goals are reviewed.
