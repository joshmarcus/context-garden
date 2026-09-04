# context-garden: design

This is the high-level design. Per-feature specs live under
`context-garden/phase-01-bootstrap/specs/`; the roadmap is `docs/roadmap.md`.

## The idea

Software gets specified by humans and built by agents. The scarce resources are human
attention and tokens. So:

- **Context lives in files, not chats.** Principles, product overviews, phase goals, specs
  and tasks are markdown in git. Anyone (human or agent) can read the same thing twice and
  get the same answer. Nothing important lives only in a conversation.
- **Agents see a brief, not a repo.** A worker gets the principles digest, the product
  overview, the phase goals, its task, and an explicit reading list. A few thousand
  tokens, deliberately assembled. If a worker needed more, the *task* was wrong, and that is
  what gets fixed.
- **No model in the scheduler seat.** Waiting, polling, ordering, retrying, rebasing and
  bookkeeping are deterministic Python. Tokens are spent only on planning, working,
  reviewing and revising, and every one of those is bounded.
- **The human does the parts only a human can do**: write goals and specs, answer the
  questions workers raise, review and merge, and decide when the loop should stop.

## Vocabulary

| term | meaning |
|---|---|
| **garden** | a repository of context: `principles/`, one directory per product, `garden.yaml` |
| **product** | something being built; has a code repo and an overview |
| **phase** | a sprint-sized slice of a product: goals, specs, docs, tasks |
| **task** | one markdown file; one agent session; one pull request |
| **brief** | the exact prompt a worker receives, assembled from the garden |
| **trellis** | the dependency and stacking structure the work climbs along (`garden trellis`) |
| **runner** | where a worker runs: `local`, `ssh`, `manual` |
| **harness** | how an agent CLI is invoked and parsed: `claude`, `codex`, custom |
| **tick** | one deterministic scheduler pass: reap, poll, dispatch |
| **persona** | a named reviewer viewpoint applied to a PR or a phase's body of work |
| **trial** | one task run by several models, compared, winner kept, scores recorded |
| **triage** | the human's first look at a draft PR: ready for review, or send back |
| **inbox** | the one list of everything that needs a person, with the action for each |
| **plant, plate** | every phase's emblem, drawn as a pressed specimen and numbered like a plate in a flora |
| **stage** | the growth-stage drawing shown beside each task state (seed, sprout, leaf, bud, flower, fruit) |

## Architecture

```
 garden files (git)            .garden/ (gitignored)            outside
 ───────────────────           ───────────────────────          ───────
 principles/00-index.md   ┐    state.json  per-task PR/stack/   GitHub: PRs, reviews, CI
 <product>/product.md     │                trial/qa bookkeeping
 <product>/<phase>/goals  ├──▶ brief ──▶ runner ──▶ harness ──▶ claude / codex (local or ssh)
 <product>/<phase>/specs  │    runs/<task>/<run>/  brief, stdout, cost
 <product>/<phase>/tasks  ┘    worktrees/<task>    one git worktree per task
                               events.jsonl       history: every transition and run
                               trials.jsonl       model scores
```

Modules, thin to thick: `cli`, `web`, `tui` render and forward; `scheduler` owns every
state transition; `graph` computes readiness, stacking and the trellis; `brief`, `review`,
`personas`, `trials` build prompts and parse the one-line JSON contracts; `runner/*` and
`harness` run things; `gitops` and `github` talk to git and GitHub; `checks` runs
token-free scripts; `events` records history; `store` and `model` read and write files.

## The loop

1. **Plan.** `garden plan product/phase` sends goals, specs, docs (including persona
   reports and the friction log) and the existing task list to one model call and writes
   task files. Each task carries a difficulty that picks the model tier.
2. **Dispatch.** `tick` fills `max_parallel` slots from the ready set: tasks whose
   dependencies are done, or (with stacking) whose one open dependency has a PR to build
   on. Phase budgets can pause this.
3. **Work.** A worker runs in a worktree with only its brief. It commits, never pushes,
   and ends with a `GARDEN_RESULT` line: done, or a question (`needs_input`), plus any
   discovered work.
4. **Gate.** Token-free pre-PR checks (tests, lint) run in the worktree. Failures become
   a revise round before a PR exists.
5. **PR.** The runner pushes and opens a draft PR (stacked on the parent branch when
   needed). An automated review checks acceptance criteria, correctness, scope and the PR
   description; configured personas add their view. Findings route into the revise loop.
6. **Triage.** The human's first look, from the Inbox: ready for review, or send it back.
7. **Humans.** Review on GitHub. Comments and red CI (analysed by token-free checkers,
   with flaky reruns) become revise runs. Stall detection stops loops that do not
   converge; questions pause the task until answered.
8. **Merge.** The task is done, dependents unblock or restack, the worktree is removed.
9. **Reflect.** `garden digest` says what needs a human; `garden metrics` says what each
   difficulty tier really cost; persona reviews of the phase and the friction log feed
   the next plan.

## Bounded loops, on purpose

Every automatic loop has a cap in `garden.yaml`: `max_attempts`, `max_revisions`,
`review.max_rounds`, `timeout_minutes`, per-phase `budgets`, plus stall detection. When a
cap is hit the task is flagged for a human rather than retried. The garden should never
be the thing that spends money while nobody is watching.

## Extension points

- **Harness**: a config block (`bin`, args, output format, tier-to-model map). Custom
  CLIs need no code.
- **Runner**: a class with `start` and `collect` (local, ssh, manual today).
- **Checks**: shell commands or `module:function` callables; results are plain JSON.
- **Personas**: a markdown file under `personas/`.
- **Everything else is a markdown file** the planner and the briefs pick up.

## Environments

One shared `garden.yaml`, plus `garden.<env>.yaml` chosen by `GARDEN_ENV` and a
gitignored `garden.local.yaml`. Home can use GitHub Actions and local workers; work can
use ssh hosts and a different CI analyser, from the same repository. The tool never
assumes a CI system; every CI-specific piece is a plugin named in config.

## Non-goals

Hosted or multi-user operation, automatic merging, and being a general workflow engine.
If the garden ever needs a database or a queue, something has gone wrong with the design.
