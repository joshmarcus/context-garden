# context-garden: architecture

How the pieces fit, and how a task moves through them. `docs/design.md` says *why* the
tool is shaped this way; this page says *how* it works; `docs/worker-protocol.md` walks
through the one conversation that matters most, between the scheduler and a worker it
spun up. Per-mechanism specs live under `context-garden/phase-01-bootstrap/specs/`.

Everything on this page is what the code does today (`src/garden/`), not a plan.

## The shape of it

Three kinds of process, one shared filesystem, one external service.

```mermaid
flowchart LR
  H((human))
  subgraph garden["garden repository (git)"]
    P["principles/00-index.md"]
    O["product.md"]
    G["goals.md, specs/, docs/"]
    T["tasks/*.md"]
  end
  subgraph dot[".garden/ (gitignored)"]
    S["state.json"]
    R["runs/ID/RUN/"]
    W["worktrees/ID"]
    E["events.jsonl"]
  end
  SCH["scheduler tick (python, no model)"]
  WK["worker process (claude -p, codex exec, ...)"]
  GH[("GitHub")]
  H -- "writes goals, specs, answers" --> garden
  H -- "CLI, web, TUI" --> SCH
  SCH -- "reads, updates status" --> T
  SCH --- S
  SCH --- R
  SCH --- E
  SCH -- "brief on stdin" --> WK
  WK -- "commits" --> W
  WK -- "JSON on stdout, exit_code" --> R
  SCH -- "push, open PR, poll" --> GH
  H -- "review, merge" --> GH
```

- **The scheduler** is one Python function, `Scheduler.tick()` in `scheduler/__init__.py`,
  with no model behind it (the class is assembled from one module per tick phase; see the
  module map below). It is called by `garden tick` (one pass), `garden watch` (a loop that
  sleeps `tick_interval` seconds between passes), `garden serve` (the same loop in a
  background thread beside the web server) and the TUI's `t` key. Every call starts by
  re-reading the task files and `state.json` from disk, so any number of these can run
  against one garden and the UIs can change state between passes.
- **Workers** are separate operating-system processes started by a *runner*: a headless
  agent CLI (`claude -p`, `codex exec`, or any CLI described under `harnesses:` in
  `garden.yaml`) running on this machine or on a host reached over ssh. They are
  detached: the scheduler keeps no handle to them and they outlive whichever process
  started them. The same transport carries reviewers, persona reviewers and trial
  comparisons; they are workers with a different brief.
- **GitHub** holds the pull requests and the review conversation. Only the scheduler talks
  to it, through the `gh` CLI when it is installed and logged in, otherwise the REST API
  with `GITHUB_TOKEN`. Workers never push and never open PRs.
- **The filesystem** carries everything between those three: the garden's markdown, the
  run directories, the git worktrees, the JSON side-store and the event log. There is no
  queue, no database and no socket.

## Module map

`src/garden/`, thin to thick: `cli`, `web`, `tui` render and forward; `scheduler` owns
every status change; `store`, `graph`, `brief`, `model` are offline. The two packages that
every feature used to edit in one place are split so that two changes in different parts
of the loop touch different files.

| module | what it holds |
|---|---|
| `model.py`, `store.py`, `graph.py`, `brief.py` | task frontmatter and statuses; discovery of products, phases and tasks on disk; the dependency graph and ready set; the worker brief and `GARDEN_RESULT` parsing |
| `scheduler/__init__.py` | `Scheduler`: construction, the shared helpers (runner, model, repo, worktree, slots), `tick()` and `_transition()`; `WORKER_MODES`, `REVIEW_MODES` |
| `scheduler/state.py`, `scheduler/report.py` | `State` (the `state.json` side-store with dirty-key merging) and `TickReport` |
| `scheduler/reap.py` | `reap`, `finalize`, the pre-PR checks and the base probe, `_after_push`, `_open_or_update_pr`, retry-or-fail, the stall, the dead-run sweep (`reap_dead_runs`) |
| `scheduler/fence.py` | the worktree fence: snapshot at dispatch, check and revert at reap |
| `scheduler/discovered.py` | discovered tasks, duplicate/cancel decision cards, friction and notes a worker reports |
| `scheduler/review.py` | the automated review round (dispatch, reap the verdict, route it), superseding a still-running review on a new dispatch, and the orphan sweep |
| `scheduler/edits.py` | the edit run that folds pending suggestions into a task body |
| `scheduler/poll.py` | `poll`: merged, closed, triage on GitHub, feedback, CI; the automerge gate; stacking, restack and conflicts |
| `scheduler/rebase.py` | rebase as its own mode: mechanical first, an agent only on a real conflict, verdict kept when the diff is unchanged, the automerge queue |
| `scheduler/dispatch.py` | `dispatch_ready`, the stuck audit, `_stack_for`, `dispatch` |
| `scheduler/human.py` | answer, accept or reject a worker decision, `mark_wont_do`, triage, cancel, retry, resume, `finish_manual` |
| `scheduler/budget.py` | phase budgets, the dispatch pause, live config overrides |
| `scheduler/upgrades.py` | the pinned tool install: note a merge, upgrade, auto-upgrade on an idle tick |
| `scheduler/aux.py`, `scheduler/trials.py`, `scheduler/persona.py`, `scheduler/retro.py` | auxiliary runs tracked in `_aux`; model trials; persona reviews; the phase retro |
| `harness.py`, `runner/` | harness definitions and output parsing; the `local`, `ssh` and `manual` runner backends |
| `review.py`, `events.py`, `trials.py`, `personas.py`, `checks.py`, `retro.py`, `friction.py`, `suggestions.py` | the review brief and verdict; the event log, digest and metrics; trial records; persona briefs and reports; token-free checks; the retro brief and documents; friction harvesting; task suggestions |
| `walkthrough.py` | render the live web app's pages to screenshots, HTML and text with an `index.md`; a phase persona review adds the newest capture to its brief |
| `gitops.py`, `github.py` | git worktrees and pushes; pull requests through `gh` or the REST API |
| `planner.py`, `plants.py`, `notify.py`, `upgrade.py`, `config.py` | the planning prompt and import; the botanical drawings; `notify.command`; the pinned install; configuration layering |
| `web/app.py`, `web/common.py`, `web/trust.py` | `create_app` and the template environment; the `Hub`, the `Site` (base template context, board data) and shared helpers; the HTML sanitiser behind `render_md` and the origin check on POSTs |
| `web/pages/` | one module per page family (`inbox`, `board`, `task`, `runs`, `trellis`, `trials`, `events`, `phase`, `config`, `api`), each registering its GET routes |
| `web/actions/` | the task-action registry (`tasks.py`: one function per action, registered by name) and the other POST routes (`control`, `phases`, `decisions`, `friction`) |
| `tui/` | the Textual TUI |
| `qa/` | `garden qa`: the throwaway garden, its fake worker and pretend GitHub (`sandbox.py`, `worker.py`), the flows as one table that is both the agent's script and the scripted run (`flows.py`), and the run itself with its report (`__init__.py`) |

## Where state lives

Git is the database. The split between the four stores is deliberate.

| store | what it holds | written by | why it is separate |
|---|---|---|---|
| `<product>/<phase>/tasks/*.md` | one task per file: YAML frontmatter is the state (`status`, `depends_on`, `priority`, `difficulty`, `branch`, `pr`, `attempts`, `last_dispatched_at`), the body is what the worker reads, `## Log` is one line per transition | humans (everything but the scheduler-owned fields), the planner, the scheduler | reviewable in git, readable by people, the source of truth for *state* |
| `.garden/state.json` | per-task bookkeeping that would be noise in a task file (below) | the scheduler and the UIs | machine detail; safe to delete and rebuild from GitHub, at the cost of one poll; concurrent writers are safe (see below) |
| `.garden/runs/<task>/<run>/` | one directory per worker run: the exact brief, raw output, exit code, usage and cost | runners and the scheduler | the audit trail and the token ledger |
| `.garden/events.jsonl` | append-only history: every transition, dispatch, run completion, review verdict, question, answer, stall, budget event | the scheduler | the source of truth for *history*; feeds timelines, `garden digest` and `garden metrics` |

Also under `.garden/`: `worktrees/<task>` (one git worktree per task, on the task's branch),
`repos/` (clones of products given as URLs), `trials.jsonl` (model trial records).
Persona reviews of a phase are written into the garden itself, under
`<phase>/docs/reviews/`, where the planner reads them next time.

### Committing task state

The scheduler edits task files in place (status transitions, attempt counters, log lines).
Those edits accumulate in the main checkout and are not committed automatically, because
committing on the user's behalf would interfere with their own git workflow. Workers branch
from `origin/main`, so state must be committed and pushed to be visible to them across
machines.

Run `garden commit` after each session (or whenever the scheduler has made edits) to stage
every modified task file and create one commit on the current branch:

```
garden commit
```

The commit message is always `garden: update task state`. `garden status` warns when task
files have uncommitted changes.

### Concurrent writes to `state.json`

`State.save()` acquires an exclusive `fcntl.flock` on a companion lock file
(`.garden/state.json.lock`), re-reads the on-disk JSON, and merges only the
keys that this process actually wrote on top of what is currently on disk.  Two
concurrent writers that touch **different keys** of the same task entry will both
survive: `garden serve` polling GitHub and `garden triage` clearing a draft flag
can run simultaneously without either change being silently lost.

If two writers change the **same key** of the same task concurrently, the last
writer wins for that key — which is fine, because individual keys are small and
owned by a single code path (e.g. only `poll()` writes `pr_updated_at`).

### What `state.json` remembers per task

| group | keys | meaning |
|---|---|---|
| the PR | `pr_number`, `pr_draft`, `pr_base`, `pr_state`, `pr_updated_at`, `head_sha`, `review_decision`, `checks`, `failed_checks`, `ci_failed_at`, `ci_reruns`, `last_polled` | what the last poll saw; `pr_updated_at` lets the poll skip PRs nothing has touched |
| the revise loop | `pending_feedback`, `revisions`, `needs_human`, `last_diff_hash`, `force_push` | feedback waiting for a revise run, how many rounds were used, why the loop stopped |
| automated review | `review_run`, `review_rounds`, `last_review`, `last_findings` | the review run in flight, rounds used, the last verdict and its blocking findings (for stall detection) |
| rebase and automerge | `rebases`, `rebase_pending`, `rebase_base`, `rebase_files`, `rebase_hunks`, `automerge_candidate`, `automerge_ready_at`, `automerge_blocked`, `automerged` | rebase rounds used (its own counter), a pending agent rebase and its hunks, whether the PR is a merge-queue candidate and since when, why automerge is held, and the record of a garden merge |
| stacking | `stack_parent`, `restack_pending` | the dependency this branch is built on, and whether to rebase when the current run ends |
| questions | `question`, `question_run`, `session_id`, `session_host`, `session_harness`, `qa` | enough to resume the paused session, and every earlier answer |
| trials, discovered work | `trial`, `worktree`, `discovered_ids` | contenders and their scores; a worktree override for the winning contender; tasks this one reported |
| suggestions | `edit_run`, `edit_attempts` | the edit run folding pending suggestions into the task body, and how many edit runs failed (capped) |

Two special entries: `_phase:<product>/<phase>` records when a budget was hit, and `_aux`
lists comparison and persona runs still in flight.

### A run directory

| file | written by | content |
|---|---|---|
| `run.json` | scheduler | task, mode, runner, harness, model, host, pid, branch, base, timestamps, status, parsed result, usage, cost |
| `brief.md` | runner | the exact prompt the worker received |
| `command.txt` | local and ssh runners | the shell command that was started |
| `remote.sh` | ssh runner | the script piped to the remote host |
| `stdout.json`, `stderr.log` | the worker process | raw harness output |
| `final.md` | harness or scheduler | the worker's final message (the `GARDEN_RESULT` line is its last line) |
| `exit_code` | the shell wrapper (or `garden finish`) | the completion signal the scheduler waits for |
| `result.json` | `garden finish` | the result of a human-driven run |

## One tick

```mermaid
flowchart TD
  A["reload task files and state.json"] --> B["reap auxiliary runs: trial comparisons, persona reviews"]
  B --> C["for each task"]
  C -->|running, in a trial| C1["reap trial contenders"]
  C -->|running| C2["reap the worker run"]
  C -->|PR open| C3["reap the review run, if one is in flight"]
  C1 --> D
  C2 --> D
  C3 --> D
  D["for each task with an open PR: poll GitHub"] --> E{"auto_dispatch?"}
  E -->|yes| F["dispatch: revise runs first, then the ready set, into free slots"]
  E -->|no| G
  F --> G["save state.json"]
```

Each step is deterministic and every branch is a plain condition on files and GitHub
responses. Errors inside one task's step are caught and reported in the tick summary so
one bad task never stops the loop.

### Reap: what a finished run turns into

The scheduler reads the run's `exit_code` file (or checks the pid) and parses the
output. Details of the transport are in `docs/worker-protocol.md`; the decisions are:

| what came back | the scheduler does | the task becomes |
|---|---|---|
| exit code not 0 and no result line | marks the run failed | `ready` again while `attempts < max_attempts`, else `failed` |
| output without a `GARDEN_RESULT` line | same | same |
| `status: blocked` | records the reason in the task log | `failed` |
| `status: needs_input` | stores the question, session id, host and harness | `waiting_human` (holds no slot) |
| `status: wont_do` or `no_change` | stores the reason and the worker's final message as a decision for the person | `waiting_human`; Accept ends a `wont_do` in the terminal `wont_do` status (closing any PR) or resumes a `no_change` to the PR/review; Reject sends it back to a revise run with the person's note |
| `status: done` but no commits ahead of the base | marks the run failed | `ready` or `failed`, as above |
| `status: done` with commits | files discovered work as tasks, commits leftovers, pushes, runs token-free pre-PR checks, opens or updates the PR, starts the automated review | `awaiting_triage` (draft PR) or `in_review`; `changes_requested` if a pre-PR check failed |
| still running after `timeout_minutes` + 5 | kills the process group | `ready` or `failed` |
| no output or worktree change for `idle_kill_minutes` | shown as "idle N min" past `idle_minutes`, then kills the process group like a timeout | `ready` or `failed` |

A failed *revise* run goes straight to `failed` (there is a PR to look at, and retrying the
same feedback rarely helps). Remote (ssh) runs are the same except that the worker pushed
the branch itself; the scheduler fetches it, checks for commits and materialises a local
worktree so review and revise runs have one.

### Poll: what GitHub tells the scheduler

For every task with an open PR:

| GitHub says | the task becomes |
|---|---|
| merged | `done`; stacked children are retargeted and rebased; the worktree is removed |
| closed without merging | `failed`; stacked children are flagged for a human |
| draft PR marked ready on GitHub | `awaiting_triage` becomes `in_review` (triage done outside the tool) |
| ready PR converted back to draft | `in_review` becomes `awaiting_triage` |
| reviews or comments newer than the task's last dispatch, from anyone but the garden's own login and bots | feedback is stored; `changes_requested` |
| the checks rollup is red | `checks.ci` analysers run; if every failure is judged flaky, the job is rerun once instead; otherwise the failing check names and the analysers' findings join the feedback; `changes_requested` |

The poll returns early when the task is already `changes_requested` (a revise run is
queued) or when the PR's `updated_at` has not changed since the last look, so an idle PR
costs one request per tick. Once `max_revisions` rounds are used, new feedback still lands
in `changes_requested`, but flagged `needs_human`: the inbox shows it and no revise run
starts until `garden retry`.

### Dispatch: filling the slots

The queue is revise runs first (tasks in `changes_requested` with feedback waiting, not
flagged for a human, under `max_revisions`), then the ready set from `graph.ready()`:
approved tasks whose dependencies are all `done`, or, with `stack: true`, whose single
unfinished dependency has an open PR to build on. Order is priority, then id. Each
candidate is skipped when no slot is free (`max_parallel` minus every active run that is
not human-driven, review and persona runs included), when its phase is over budget, or when
its runner is `manual` (a person takes those with `garden take`).

Dispatching one task means: choose the runner (task, then product, then garden default),
the harness (same order), the model (an explicit `model:`, else the harness's tier map by
`difficulty`), the base branch (a stack parent's branch or the product base), prepare the
worktree, build the brief, write the run record, start the process, then record
`attempts`, `last_dispatched_at` and the `running` transition.

## The task state machine

```mermaid
stateDiagram-v2
  [*] --> draft: planner, garden new-task, discovered work
  draft --> ready: garden approve (or the planner, with auto_approve)
  ready --> running: dispatch
  running --> awaiting_triage: done, draft PR opened
  running --> in_review: done, PR opened ready (draft_pr off)
  running --> waiting_human: needs_input, wont_do, no_change
  waiting_human --> running: garden answer, session resumes
  waiting_human --> wont_do: accept a wont_do call
  waiting_human --> changes_requested: reject a wont_do / no_change call
  waiting_human --> in_review: accept a no_change call
  running --> ready: crash or no result, attempts left
  running --> failed: blocked, attempts used, push failed
  running --> changes_requested: pre-PR check failed
  awaiting_triage --> in_review: triage, ready for review
  awaiting_triage --> changes_requested: triage, send back
  awaiting_triage --> changes_requested: automated review asks for changes
  in_review --> changes_requested: new comments, red CI, review verdict
  changes_requested --> running: revise run
  in_review --> done: PR merged
  in_review --> failed: PR closed unmerged
  failed --> ready: garden retry
  done --> [*]
```

`blocked` is never stored; it is computed from `depends_on` for display. `cancelled` is
reachable from anywhere with `garden cancel`. `wont_do` is terminal, counted as neither done
nor failed (nor in the inbox): a person accepted a worker's call that the task should not be
done. Only the scheduler writes `status` (the CLI and UIs go through it), which is why task
files under `tasks/` must not be hand-edited.

## Git and the pull request

- **Repositories.** A product's repo is a path relative to the garden or a URL; URLs are
  cloned once under `.garden/repos/`. The GitHub slug comes from the `origin` remote
  unless `products.<name>.github` overrides it.
- **The garden as its own product.** A product may point at the garden's own repo
  (`products.<name>: {repo: <the garden's origin>, self: true}`). Its tasks change the
  garden's own files — a phase's friction document, the next phase's goals, the product
  overview, `garden.yaml` — and are dispatched like any other task: the worker gets a
  worktree of the garden repo under `work_dir`, edits there, and the change comes back as a
  PR to the garden repo, with the same fence, checks and review. **The live garden is never
  edited by a worker; changes to it arrive by PR like everything else**, and the running
  garden picks them up when the person merges and `garden sync` pulls. Two guards keep the
  worktree apart from the live checkout: `garden doctor` refuses a `work_dir` inside the
  live garden (and a `self` repo that resolves to the live garden root), so the clone and
  worktrees sit outside it; and the fence (`find_root`) resolves a worker's garden worktree
  to that worktree's own `garden.yaml`, never the enclosing live garden.
- **Branches and worktrees.** A task's branch is `garden/<id>-<slug>`. Its worktree is
  created from `origin/<base>` (or the local base when there is no remote) and reused
  across runs of the same task. The worker only ever commits in the worktree; the
  scheduler pushes with `git push -u origin HEAD:refs/heads/<branch>`, force-with-lease
  only after a rebase.
- **Draft first.** With `github.draft_pr` (default on) every PR opens as a draft and the
  task waits in `awaiting_triage` for the human's first look, while the automated review
  and any configured personas run against it. Triage marks it ready (on GitHub too) or
  sends it back with a note that becomes the next revise brief. When a review round is
  still coming, the triage notification (see `notify.command` below) waits for that
  verdict instead of firing the moment the draft opens, so the ping arrives with the
  review's read on the PR already attached.
- **Stacking.** A task whose one unfinished dependency has an open PR starts from that
  branch, its PR targets that branch, and `state.json` records `stack_parent` and
  `pr_base`. When the parent merges, the child's PR is retargeted to the product base and
  its branch rebased and force-pushed; a textual conflict starts a rebase round (below). A
  parent closed without merging flags the children for a human.
  - **Automerge only into the product base.** Automerge (see below) merges a PR only when
    its base is the product's base branch. A stacked child (its base is the parent's
    branch) is held with the reason `stacked on <parent>; waits for the restack`: it must
    wait for the parent to merge and for its own branch to be restacked onto the base
    before it can automerge. Merging a child into the parent's branch would put commits
    there that the parent's worktree does not have, and the parent's next rebase round
    would force-push them away.
  - **A rebase round keeps remote-only commits.** A rebase round rewrites a branch in the
    worktree and force-pushes it, so before rebasing the scheduler folds in any commits
    that exist only on `origin/<branch>` by rebasing the worktree's commits onto it first.
    This means a force-push never discards work that reached the remote branch by another
    route (someone merged into it). If those commits conflict, the round resolves them like
    any other conflict.
- **Rebase is its own mode** (`scheduler/rebase.py`). A PR that falls behind its base is
  brought forward by the cheapest thing that works, tracked as its own kind of run, and
  never re-reviewed for code the reviewer already approved. Three rules:
  - **Mechanical first, an agent only on a real conflict.** When a PR conflicts (GitHub
    reports `CONFLICTING`, a parent merged, or the merge queue is about to land it) the
    scheduler runs `git rebase origin/<base>` in the worktree with no model. A clean apply
    is the whole round: a `rebase` run record with no harness call, a force-push with a
    lease and a re-run of the pre-PR checks. Only a textual conflict starts an agent, on the
    easy tier, with a minimal brief carrying the conflicting hunks, the task's goal and the
    rule "resolve the conflict, change nothing else". A rebase round has its own counter
    (`state[task].rebases`), its own line in `garden metrics` (rebases per merge, rebase
    cost), and never counts against `max_revisions` or `review.max_rounds`.
  - **No re-review when the diff is unchanged.** After any rebase the diff against the new
    base is compared with `last_diff_hash` from the reviewed push. When they match, the last
    verdict is kept, `rebased; diff unchanged; verdict kept` is logged, and no review is
    dispatched. Only a textual resolution that changed the diff is reviewed again.
  - **Automerge is a queue.** Approved candidates are ordered oldest-approved-first and only
    the head is rebased, checked and merged; the next candidate is taken on the following
    poll. So exactly one PR is rebased and merged per tick, each rebased once, right before
    it merges.
- **A broken base parks, then continues on its own** (`scheduler/reap.py`). When a pre-PR
  check fails, the scheduler probes the branch's base commit before spending a revise round.
  If the same check fails at the base too and the base branch has **not** moved, the base is
  itself broken: the task parks with a `base_broken` stop (recording the base branch and its
  probed tip) — no revise round, no worker, no spend. Every following tick re-probes: while
  the base tip is unchanged it just waits, but the moment the base branch goes green the
  scheduler rebases the branch onto it mechanically (a no-cost `rebase` run, the same path as
  above), force-pushes so any stale CI on the branch runs again, re-runs the pre-PR checks and
  — on green — clears the stop and opens or updates the PR, all without a worker. A rebase that
  does not apply, or checks that still fail after it, fall through to the normal revise path
  (and only then); a base that moved but is still red simply re-parks against the new tip. The
  event is `rebased_stale_base`.
- **Feedback detection.** Reviews, line comments and issue comments newer than the task's
  `last_dispatched_at` count, minus the garden's own comments (recognised by a hidden
  marker) and the accounts in `github.bot_logins`, so the scheduler's own review comments
  never trigger a revise run. The revise brief carries only those new items, not the whole
  thread.
- **Only trusted authors prompt a worker.** A comment is text a worker would carry out, and
  on a public repo anyone can leave one. `GitHub.is_trusted` admits the login the garden
  authenticates as, `github.trusted_authors`, the `github.reviewers` it requests, and
  `[bot]` accounts (review apps the owner installed). Everything else, a `CHANGES_REQUESTED`
  review included, is returned as `ignored` with the reason `untrusted`, logged once on the
  task with a `feedback_ignored` event, and never reaches a brief. The GitHub review
  decision still gates automerge, so an untrusted request for changes blocks a merge
  without steering a worker.
- **CI.** The scheduler reads the checks rollup on the PR head, whichever system posts
  it. Log analysis is whatever `checks.ci` names; nothing assumes GitHub Actions.

## Every kind of run

All of these go through the same runner transport; they differ in the brief they get and
the marker line the scheduler looks for at the end of their output.

| mode | started by | brief | ends with | model | what happens with the output |
|---|---|---|---|---|---|
| `work` | dispatch | `build_brief`: rules, principles digest, product overview, phase goals, task, reading list | `GARDEN_RESULT` | task difficulty tier | push, PR, review run |
| `revise` | dispatch, when feedback is pending | the same, plus a "Revision round" section and the new feedback | `GARDEN_RESULT` | task tier | push, PR title and body updated, a comment on the PR, review run |
| `resume` | `garden answer` | the answer, into the paused session (`--resume`); a fresh brief with every Q&A when the harness cannot resume | `GARDEN_RESULT` | task tier | as `work` |
| `rebase` | a real (textual) rebase conflict; the mechanical rebase runs with no model and needs no worker | a minimal brief: the conflicting hunks, the task's goal, "resolve the conflict, change nothing else" | `GARDEN_RESULT` | easy | push (lease), re-run checks; the verdict is kept when the diff is unchanged, else a review runs. Own counter; never counts against `max_revisions` |
| `review` | after a PR is opened or updated | the task brief without rules, the PR title and body, the diff | `GARDEN_REVIEW` | `review.difficulty`, or the harness's `review_model`, else the task tier | verdict posted as a PR comment; `request_changes` becomes feedback for a revise run; a repeated blocking finding stalls the loop |
| `edit` | dispatch, when a draft/ready task has pending `## Suggestions` (or `garden integrate`) | the task body and the suggestions, planner-style | `GARDEN_EDIT` | `review.difficulty`, else the task tier | the task body is rewritten to fold in the suggestions, they are marked `- [x]`, the old body is kept for the diff |
| `persona` | `garden persona-review`, or `review.personas` on every PR round | the persona file plus the phase's body of work, or plus one PR's description and diff | `GARDEN_PERSONA` | review settings, `hard` for a phase | a report under `<phase>/docs/reviews/`, or a PR comment; high findings can become tasks or a revise run |
| `trial` | `garden trial` | as `work`, once per contender on its own branch | `GARDEN_RESULT` | the contender's model | each contender pushes and gets a PR |
| `compare` | when every contender has finished | the task brief, every contender's PR description and diff | `GARDEN_COMPARE` | review tier | the winner's branch and PR become the task's; the others are closed with the ranking posted |
| `retro` | `garden retro`, once the phase's persona reviews are in | the harvested PR-body friction, the persona reports, the phase's task list with statuses, the merged PR titles | `GARDEN_RETRO` | review tier | the retro document and the next phase's goals draft are rendered from the verdict list and opened as a PR to the garden's own (`self`) repo |
| planner | `garden plan` | goals, specs, docs, persona reports, existing tasks | a JSON array | `hard` | task files; the only synchronous call, made from the garden root, not a worktree |

## Interfaces

All three are thin. They read `Store`, `State`, `RunStore` and `EventLog`, call methods on
`Scheduler` (`dispatch`, `answer`, `triage`, `cancel`, `retry`, `start_trial`,
`dispatch_persona_*`, `tick`) and render.

- **CLI** (`cli.py`, Typer): every operation, scriptable; `garden inbox` and `garden
  digest` are the text versions of the home page.
- **Web** (`web/`, FastAPI and Jinja templates): the Inbox, Board, Trellis, Timeline,
  Trials, Runs, task and phase pages. `web/app.py` builds the app and the template
  environment; each page family under `web/pages/` and each action module under
  `web/actions/` registers its own routes (`register(app, site)`), and the task actions
  are a registry (`web/actions/tasks.py`: one function per action, `@action("name")`,
  and `POST /tasks/{id}/{action}` is a table lookup). No build step and no CDN: charts
  and the trellis are server-rendered SVG, live regions poll a partial every few seconds.
  `garden serve` runs the scheduler loop in a background thread unless `--no-watch`.
  Two checks sit at its edges (`web/trust.py`): every piece of markdown a page renders
  (task bodies, PR feedback, review verdicts, persona reports, specs) is reduced to an
  allowlist of tags and attributes with safe link targets, since much of it was written by
  an agent or a commenter; and a POST whose `Origin` (or `Referer`) is another site is
  refused with 403, so a page open elsewhere in the same browser cannot press the buttons
  (`web.trusted_origins` lists any extra origin a reverse proxy presents).
- **TUI** (`tui/app.py`, Textual): an Inbox tab and a Tasks tab with the same actions,
  refreshing every few seconds so it can sit beside a `garden watch`.
- **Skills** (`.claude/skills/`, written by `garden init`): `garden-take`, `garden-plan`
  and `garden-review` let an interactive Claude Code session act as a worker, planner or
  reviewer through the manual runner; `garden-operate` is the operator's playbook for a
  running loop. A person can pair on a task, or run the loop, without leaving it.

## Configuration and environments

`Config.load` merges `DEFAULTS` (in `config.py`), then `garden.yaml`, then
`garden.<GARDEN_ENV>.yaml`, then a gitignored `garden.local.yaml`. Dictionaries merge and
lists and scalars replace, so an overlay can swap the whole `checks.ci` list or the ssh
host list without touching the shared file. Per-product blocks under `products:` override
`repo`, `base_branch`, `id_prefix`, `runner`, `harness`, `budget_usd` and `github`.
`garden doctor` prints which files were loaded and what it found.

Every automatic loop has a cap here: `max_attempts`, `max_revisions`,
`review.max_rounds`, `timeout_minutes`, `idle_kill_minutes`, `budgets`, `stall.enabled`.
Hitting a cap flags the task for a human instead of retrying.

**`notify.command`** (`src/garden/notify.py`) is a shell command the scheduler runs
whenever a task needs a human: `awaiting_triage` (once a pending review's verdict is
known — see "Draft first" above), `waiting_human`, `failed`, `changes_requested` past
`max_revisions`, plus `stalled`, `needs_human` and `budget` events. It gets the task in
environment variables — `GARDEN_TASK_ID`, `GARDEN_STATUS`, `GARDEN_MESSAGE`, `GARDEN_PR`
— and `notify.timeout_seconds` (default 30) bounds how long it may run. It is empty by
default (no notifications); see `notify:` in `examples/garden.work.yaml` for a working
example to copy. `garden doctor` runs the configured command for real, with a synthetic
`GARDEN_TASK_ID=DOCTOR-TEST` payload, and reports whether it exited zero — a broken
command (typo, missing binary, unreachable webhook) is caught there rather than the first
time a task actually needs a human. At runtime, a command that exits non-zero, times out
or fails to start does not stop the scheduler, but is logged as a warning (logger
`garden.notify`) instead of failing silently.

## Extension points

| to add | provide | code needed |
|---|---|---|
| a harness (another agent CLI) | a block under `harnesses:` with `bin`, `command` or argument shape, `output` format, a tier-to-model map, optional `resume_command` | none |
| a runner (another place to run) | a subclass of `runner.base.Runner` with `start` and `collect`, registered in `runner/__init__.py` | one class |
| a check (token-free) | `{name, command}` or `{name, python: "module:function"}` under `checks.pre_pr` or `checks.ci`; helpers in `checks.py` for log analysers | none, or one function |
| a persona | a markdown file under `personas/` | none |
| context | markdown under the garden; the planner and the briefs pick it up | none |

## How it is tested

`tests/fake_claude.py`, `fake_codex.py` and `fake_ssh.py` stand in for the real
binaries: the fake harness takes the brief, commits something in the worktree and returns
a `claude -p --output-format json` shaped result, with an environment variable choosing
the scenario (done, crash, no result line, blocked, needs a decision, discovered work, a
revise round that changes nothing, a rebase conflict). The scenarios are two tables in
that file: `SPECIAL` for runs that are not a worker round (crash, stall, the planner, a
comparison, a persona, a retro, an edit, the `review-*` verdicts) and `WORKERS` with one
row per worker mode; a new scenario is a new row.

No test drives a subprocess worker. `tests/inprocess.py` is a `LocalRunner` whose launch
step calls the fake harness as a Python function instead of spawning it: it prepares the
same setup, brief, environment and resolved argv, writes the same `stdout.json`,
`stderr.log`, `command.txt` and `exit_code` beside the run record, and returns with the
run already finished. An autouse fixture in `tests/conftest.py` puts it in the runner
registry under `local` for every test, so a test ticks once to dispatch and once to reap,
with nothing to wait for and nothing left running; a `stall` worker is a run with no
`exit_code`, which is what the idle and timeout checks look for. The local runner's own
launch mechanics are tested by constructing `LocalRunner` with `subprocess.Popen` stubbed;
the ssh end-to-end test is the one place a real command runs (its remote script is shell),
and it waits on that child directly rather than polling.

The `garden` fixture in `tests/conftest.py` builds a garden with one product whose repo is
a local git repo with a bare `origin`, and a fake GitHub records PRs, comments and feedback
in memory. That is enough to drive every state transition end to end without a network.
The scheduler's own tests sit under `tests/scheduler/`, one file per tick phase
(`test_reap.py`, `test_poll.py`, `test_dispatch.py`, `test_human.py`, `test_notify.py`,
`test_orphan_sweep.py`, `test_dead_runs.py`) with shared helpers in `tests/scheduler/conftest.py`. `pytest -q`
runs it all in well under a minute; CI for this repository runs the same in
`.github/workflows/ci.yml`.

## Rules the code keeps

- `model`, `store`, `graph` and `brief` make no network calls and no subprocess calls
  beyond git, so briefs and readiness are testable offline.
- Only `scheduler` changes a task's status; the CLI, web and TUI call it.
- Workers commit and never push; the scheduler pushes and never commits code of its own
  (it only commits a worker's leftover changes before pushing).
- A worker runs in a scrubbed environment (`runner.base.scrubbed_env`): an allowlist of the
  scheduler's variables plus `worker_env.pass` and the product's `setup.env`, never its
  GitHub token, cloud credentials or ssh agent. The ssh runner's remote worker gets the
  remote login environment, which is that host's to set.
- No model runs in the tick. Waiting is a sleeping Python process.
