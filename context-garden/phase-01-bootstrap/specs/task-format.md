# Task file format

One markdown file per task under `<product>/<phase>/tasks/`, named `<ID>-<slug>.md`.
YAML frontmatter carries state; the body carries the brief-facing content.

```yaml
---
id: CG-007
title: Add an automated review pass
status: draft            # draft | ready | running | in_review | changes_requested | done | failed | cancelled
product: context-garden
phase: phase-02-friction
depends_on: [CG-003]     # task ids; `blocked` is derived, never stored
priority: 2              # 1 = highest; ties broken by id
estimate: M              # S | M | L, informational
reading:                 # garden-relative paths inlined into the brief (dirs allowed)
  - context-garden/phase-02-friction/specs/review-pass.md
repo: ""                 # optional override of the product repo
branch: garden/cg-007-add-an-automated-review-pass   # set on first dispatch
pr: https://github.com/...                            # set when the PR is opened
runner: ""               # optional override: local | ssh | manual
harness: ""              # optional override: claude | codex | ...
difficulty: medium       # easy | medium | hard -> model tier (see harness.md)
model: ""                # explicit model override
attempts: 0              # work runs so far
last_dispatched_at: ""   # ISO timestamp; review feedback newer than this is "new"
created: 2026-09-04T00:00:00+00:00
updated: 2026-09-04T00:00:00+00:00
---

## Goal
## Context
## Acceptance criteria
## Out of scope
## Log            # appended by the scheduler; one timestamped line per transition
```

Unknown frontmatter keys are preserved. `attempts`, `last_dispatched_at`, `branch`, `pr`
and the `## Log` section are written by the scheduler; humans edit everything else.

## Status machine

```
draft --approve--> ready --dispatch--> running --worker done, PR opened--> in_review
in_review --feedback / CI red--> changes_requested --dispatch(revise)--> running
in_review --PR merged--> done
in_review --PR closed--> failed
running --worker failed, attempts < max--> ready
running --worker failed, attempts >= max, or blocked--> failed
* --cancel--> cancelled
```

`blocked` is shown for a `draft` or `ready` task whose dependencies are not all `done`.
