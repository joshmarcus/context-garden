# Now 1: the design

The design for `/now1`, the first of the two Now pages (spec:
`context-garden/phase-05/specs/now-page.md`, section "Two Nows"): the page a person leaves
open on a second screen and shows in a demo. It fixes the layout and hierarchy of the four
regions, the visual system, the motion, every state, and the data each element reads, so the
build task has one target and no design decision left to make. A static mock rendered from a
real snapshot of this garden is at `docs/design/now-1.html`; how it was made is at the end.

The page is called "Now 1" only in the nav, while Now 2 exists beside it; its heading is
"Now". Nothing in the design depends on Now 2.

## In one sentence

The page is a herbarium sheet in progress: at the top the specimens being pressed right now
(the runs), under them the ones queued for the press (Next), beside them the phase's plant
inked as far as the phase has got (Where we are), and along the bottom the ledger of the last
period. Nothing on it is a card of equal weight; the order of prominence is the layout.

## The five-second answer

Under the title, one sentence in the page's serif states what the page is for, filled from the
same data as the regions, each clause a link to its region:

> 8 runs in flight on 5 of 5 worker slots and 3 of 3 review slots. Next: CG-249, then CG-292.
> phase-05: 3 of 37 merged. Last hour: 3 merged, $28.08.

A visitor who reads nothing else has the four answers. The sentence rewrites itself from the
same live updates that move the regions (see Live updates), so it never disagrees with them.
When dispatch is paused or a harness is paused the sentence says so first ("Dispatch paused by
cli since 00:31."), because that is the one fact that changes what the rest means. When the
busy count exceeds the slot count because run records without a process are holding slots
(see States), the sentence says so in the same breath: "6 of 5 worker slots (2 without a
process)". It never rounds the truth down to make the numbers tidy.

## Layout

At 1280 wide (a projector), inside the app's shell with the rail on the left:

```
┌ rail ┐ ┌──────────────────────────────────────────────────────────────────────┐
│ Now 1│ │ Now                                                                  │
│ Now 2│ │ 8 runs in flight … Next: … phase-05: … Last hour: …                  │
│ Inbox│ │                                                                      │
│ …    │ │ NOW                                    5 of 5 slots · 3 of 3 reviews │
│      │ │ ┌ strip: glyph id title · mode · harness model                   ┐   │
│      │ │ │        elapsed ▬▬▬▬▬▬│──  14 min · typically 47   $0.42 so far │   │
│      │ │ │        "the last thing the worker said"                        │   │
│      │ │ └────────────────────────────────────────────────────────────────┘   │
│      │ │ ┌ strip ┐ ┌ strip ┐ … (one per run, stacked, newest first)          │
│      │ │                                                                      │
│      │ │ NEXT  next tick in 0:42          │ WHERE WE ARE                      │
│      │ │ 1. CG-249 title                  │ ┌ specimen sheet ─────────┐       │
│      │ │    revise round 1 of 3 · easy →  │ │  plant, inked to 8 %    │       │
│      │ │ 2. CG-292 title                  │ │  label: plate V · 3/37  │       │
│      │ │ …                                │ └─────────────────────────┘       │
│      │ │ Merge queue: head, candidates    │ goals with a mark each            │
│      │ │ In review: round, CI, held on    │ other open phases · closed row    │
│      │ │ Reviews waiting for a slot       │                                   │
│      │ │                                                                      │
│      │ │ THE LAST PERIOD   hour · today · 24h · this phase                    │
│      │ │ merged 3   first pass 67 %   cost $28.08 ▁▂   per accepted $3.27     │
│      │ │ [ cost by activity, hourly, stacked ]   runs by harness and model    │
│      │ │                                          hand steps 3                │
│      │ └──────────────────────────────────────────────────────────────────────┘
```

Now spans the full width because it is the region that moves. Next and Where we are share
a row on the `grid2` idiom the app already uses (1.7fr : 1fr): the queue is a list and needs
the width; the sheet is a portrait and does not. The last period is full width again because
its chart wants the width and it is read last.

The rail is the app's rail, with one change: its Running now block is hidden on this page
(a `page-now` body class and one rule), because the page is that block. The Phases drawers,
the operating profile and the tick line stay.

At 390 wide (a phone) the same four regions in the same order, one column. The rail
collapses to its wordmark and the nav row (a rule scoped to `page-now` hides the rail's other
blocks under 1000px; every other page keeps today's rail). A run strip wraps onto four lines:
id, title, then mode and model, then the elapsed bar and spend, then the last words clamped
to two lines. The sheet's label sits under the plant instead of over it. The period's four
figures become a 2 by 2 grid, the two small tables stack, and the cost chart is the narrow
rendering (see The last period).

The page is read-only. No button on it changes state; the Inbox and Board are for acting
and every id links there.

## Visual system

Everything is the herbarium's and nothing is new chrome.

- **Ground and surfaces.** The paper ground (`--ground`), the fixed background vine, no
  boxes around regions. A region is an eyebrow (`.eyebrow`, mono, uppercase) with a short
  serif line beside it, then its content. The only bounded surfaces are the run strips and
  the specimen sheets, all `.specimen` (label paper, hairline, the mount shadow, a piece of
  tape).
- **Type.** Newsreader for the five-second sentence, task titles, figures and the worker's
  last words (italic); Courier Prime for ids, modes, harness and model, elapsed and spend,
  the queue positions and every label. Figures use tabular numerals (already on `body`).
- **Glyphs.** The growth-stage drawings from `plants.py` say what a run is doing:

  | run mode | glyph | word |
  |---|---|---|
  | work, revise, resume, trial | `st-leaf` | in leaf |
  | rebase, edit | `st-sprout` | sprout |
  | check | `st-bud` | in bud |
  | review, persona, compare | `st-flower` | in flower |
  | retro, kickoff | the phase's plant thumbnail | (phase-level) |
  | finished: done / approve | `st-fruit` | in fruit |
  | finished: failed, timeout | `st-wilt` | wilted |
  | finished: needs_input | `st-tag` | bud, tagged |
  | finished: cancelled, superseded | `st-pressed` | pressed |

  The word is in each glyph's `<title>`, never the only signal (as everywhere else). In the
  Next list a ready task carries its own stage glyph (sprout) and a revise or rebase round
  the pruned glyph (`st-cut`), the same as the Board.
- **Colour only for state.** The 8px `.state` dot, with the app's status colours:
  `--s-running` on a worker run, `--s-in_review` on a review or persona run, `--s-ready` on
  a check or rebase, `--s-failed` on a failed run, `--s-waiting_human` on a needs-you card,
  `--s-blocked` (amber) on a paused harness, the faint grey on a record without a process.
  The rubber stamp (`.stamp`, stamp red) carries the four words that need a person or explain
  a stop: **held**, **paused**, **needs you**, **failed**. Nothing else on the page is
  coloured except the one cost chart, which keeps the Costs page's activity palette so the
  two pages agree.
- **One accent for movement.** `--accent` (moss) is used for exactly three things, all of
  them growth: the elapsed fill on a run strip, the fresh-ink wash on the phase plant when a
  task lands, and the left rule of a strip for its first three seconds. It appears nowhere
  static.
- **Dark mode** follows the app: every colour is a token from `base.html`; the plate mount
  keeps its paper as on the phase page.

## The regions

### Now

Every run in flight, newest first, one strip each. A strip is a mount card lying on its
side, about 96px tall at 1280:

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ● [leaf]  CG-307  Design the Now page …      trial · claude claude-fable-5-1     │
│           ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬│╌╌╌╌      31 min · typically 18     $0.42 so far       │
│           "Found a Windows Chrome reachable from WSL, so screenshots …"           │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Elements, left to right and top to bottom:

1. The state dot and the mode glyph (table above), then the id (`.id`, links to the task
   page) and the title in serif.
2. On the right of the first line: the mode word, the dispatch stage if the run has one
   (`revise · rebase`), and the harness and model in mono (a check run shows `check ·
   pre_pr`; a trial shows `trial` and its contender).
3. The elapsed bar: a scale bar like the one on a plate. A hairline track 160px long stands
   for the typical duration of this mode and tier, with an end mark; the accent fill grows
   along it from the left. When elapsed passes typical the fill stops at the end mark and a
   dashed hairline grows on past it, to at most half a track more, and the text says `31
   min · typically 18`. The dashed line has its own room beyond the end mark; it never runs
   into the text. No colour changes: an overrun is information, not an alarm. With fewer
   than three finished runs to take a typical from, there is no bar, only `14 min`. A
   duration under 90 seconds reads in seconds (`40 s`), so a mechanical rebase never says
   `typically 0 min`.
4. Spend so far in mono, `$0.42 so far`, priced from the run's stream with the harness's
   price table. When the table has no entry for the model (the claude harness ships without
   one; only codex has list prices today) the strip shows the tokens instead, `163k tokens
   so far` (`2.5M` past a million; cache reads count, since they are what a long run is
   made of), which is real, moves, and says what the dollars would be made of; `no usage yet`
   in the faint colour while the stream has none, and `token-free` for the garden's own
   runs (a check, a mechanical rebase), which never will. The choice is dollars if they are
   known, else tokens, never an estimate.
5. The last thing the worker said: the newest assistant text in the run's stream, first
   line, clipped at 120 characters, in italic serif. `no words yet` in the faint colour
   while the stream has none.

Order: newest dispatch first, so a strip arriving slides in at the top and older strips
settle down the page. Runs for the same task (a trial's two contenders, a revise and a
record left by an earlier round) are kept adjacent, the older one first, and the group
takes the position of its newest run.

A run record without a process: a `run.json` with status `running`, no pid and no output,
written at dispatch and never launched. The scheduler counts it against a worker slot until
a tick reaps it, so the page shows it, but as what it is: the same strip with a dotted rule,
the title and glyph at half strength, the grey dot titled "no process", and in place of the
bar and the words one mono line, `no process recorded · started 01:01Z · a slot is held
until a tick reaps it`. Hiding it would make the slot count lie.

Cards that are not runs but belong here because a person is waiting on them, after the
runs, each a strip with a stamp instead of a bar. Their words are the Inbox's, so the two
surfaces agree:

- **held**: a PR in the Inbox's *Review and merge* group that is not merging
  (`automerge_blocked` set), the reason as the strip's second line ("the automated review
  verdict is request_changes, not approve"). Only when the reason names a person's action
  (the review cap, CI red twice, a human review decision); a queue that is merely waiting
  for its rollup or its second review round is in Next.
- **paused**: the Inbox's *Harness paused* notice (`_control.paused_harnesses`), with the
  reason and when it was paused; the probe that resumes it runs on its own, and the strip
  says so.
- **needs you**: the Inbox's *Answer a worker's question* and *Needs a decision* groups: a
  task in `waiting_human` (its question, italic) or with a `needs_human` stop (the stop's
  reason). The strip links to the task, where the answer form is.

Slots, on the region's right: `5 of 5 worker slots · 2 of 3 review slots`, from the
scheduler's `worker_runs_active`, `check_runs_active`, `effective_max_parallel`,
`review_runs_active` and `review_parallel_limit`. When dispatch is paused the region header
carries the **paused** stamp and `dispatch paused by cli at 00:31 · reason`.

### Next

The order the scheduler will take work in on its next tick, as it will actually take it,
numbered, with why each item is where it is. It is the queue that `dispatch_ready` builds,
not a filtered board:

1. Rebase rounds (`changes_requested` with `rebase_pending`), because they are the cheapest
   work and unblock a merge; "rebase round, goes first".
2. Revise rounds (`changes_requested` with `pending_feedback`, under `max_revisions` or a
   rebase-exempt round); "revise round 2 of 3".
3. Ready tasks in `dispatch_sort_key` order; "priority 1", "priority 2 · order 3".

Each line has two rows: position, the stage glyph the task has now (sprout for ready,
pruned for a revise or rebase), id and title on the first; on the second, in mono, why it is
here, the mode, the tier and the harness and model it would get (from `Scheduler.runner_for`
and the harness's tier map): `priority 1 · work · easy → claude claude-sonnet-5`. The title
gets the whole width, so it reads in one line at 1280. A line the tick would skip says so
in bold amber in place of the harness: `phase frozen`, `over budget`, `harness paused`,
`manual runner`; it stays in the list because the order is still true. Eight lines, then
`and 14 more →` to the Board's backlog. Above the list: `next tick in 0:42` counting down
from the last tick plus `tick_interval`, and `1 slot free` or `no slot free: the first line
waits for a run to finish`.

Then the merge queue, from `inbox.merge_queue_view` and state, one row per PR with the id
on the left and the fact under the title:

- the head, if any: `CG-245 rebased, waiting for its rollup · CI pending` (or `CI green:
  merges on the next tick`);
- candidates in `automerge_ready_at` order, each with its hold reason if one is set;
- every other `in_review` task: `round 1 of 4 · CI success · review running` or `· only 1
  review round so far, need 2`, so nothing with an open PR is invisible.

Then reviews waiting: each task's `pending_reviews` entries (`review`, `persona:security`)
with why they wait: `no review slot (3 of 3 busy)` or `harness paused`.

### Where we are

The current phase as a specimen sheet, the way the phase page mounts it, with one change:
the plant is inked only as far as the phase has got. Two copies of the plant (the scanned
plate when fetched, else the drawing) sit on top of each other: the lower at 14% opacity
and greyed, the upper clipped from the top with `clip-path: inset(<100 − done/total %> 0 0
0)`. The plant rises from the ground as tasks merge, and the full height is the phase's
scope, so a glance says how far and how much is left. The typed label (`.lbl`) under the
tape: product and plate, phase name, the Latin name, `3 of 37 merged · 7 PRs open · 7
running`, spend against budget when one is set, and the growth-stage word for the fraction
(0 seed, under 25% sprout, under 50% in leaf, under 75% in bud, under 100% in flower, all in
fruit). The stamp says **open**, **frozen** or **complete**; a recorded retro verdict adds a
second stamp with the verdict word (`reopen`, `close`) and, below the sheet in mono, its
status and who accepted it and when.

The goals, from the numbered list under `## Goals` in the phase's `goals.md`: one line per
goal, its first bold phrase or, failing that, its first sentence, with a mark: `st-fruit`
when every task the goal names by id is done, `st-leaf` when any is running or has a PR,
`st-seed` otherwise, and `n of m` in mono with the word (`merged`, `in flight`, `not
started`). A goal that names no task id gets no mark and the faint word `unlinked`; the goals
text is the source and the ids are the link, and the design does not invent one.

Which phase is "current": the open phase with the most runs in flight, ties broken by
name; the other open phases follow as small sheets (thumbnail, the same label, **frozen**
when frozen). Closed phases are a row of thumbnails with their plate numbers and the
**pressed** stamp, newest first, as the Herbarium shows them; each links to its phase page.

### The last period

A window, then the numbers. The window links are `.filters`: `last hour` (default),
`today` (UTC midnight, as the Costs page defines it), `last 24 hours`, `this phase` (from the
current phase's first dispatch). Four figures in serif on one line, each with its mono
label under it:

- **merged**: `transition` events to `done` in the window, counted once per task at its
  latest such event (the burn-up's rule); the label names the first four ids and `and 98
  more`;
- **first pass**: of the tasks whose first `review` event is in the window, the share with
  verdict `approve`, shown as `67 % · 2 of 3`;
- **cost**: the sum of `run_finished` cost in the window, plus the operator ledger's entries
  in the window (`operator_spend.to_cost_events`), so it agrees with the Costs page; beside
  it the run count and a sparkline of runs finished per bucket, the label saying so (`cost ·
  runs finished per hour`);
- **per accepted task**: cost per accepted task as `garden metrics` reports it once CG-251
  lands (the same field the Costs page shows); until then the cost of the tasks merged in
  the window divided by their count, and `—` with no merge.

Beneath, left: cost by activity as an hourly stacked bar (`charts.cost_stack_svg` with
`bucket="hour"`, grouped by activity, for `today` and shorter windows; daily for `this
phase`), with the Costs page's annotation marks for `profile_changed`, and under the chart
one mono line listing the window's `profile_changed` and `config_reloaded` events with the
keys that changed. The chart is rendered twice, at 640 and at 360 wide, and CSS shows one
per viewport, so the phone gets legible labels rather than a scaled-down projector chart;
the two SVGs together are a few kilobytes. Right: runs by harness and model (`run_finished`
grouped by `harness:model`; the garden's own token-free runs group as `garden:check`,
`garden:rebase` and so on: runs, cost, mean), then hand steps: the count of events in the
window whose kind is a person's action (`answer`, `triaged`, `decision_accepted`,
`decision_resolved`, `dispatch_paused`, `dispatch_resumed`, `resumed`, `moved`, `budget_set`,
`config_override`, `suggestion`), by kind, and hand merges from `garden metrics` once CG-253
lands (`—` until then, and the label says why).

Every number here is a function in `events`, `costs`, `charts` or `operator_spend` called
on the window's events. The template formats; it never computes.

## Motion

The page must feel alive without a reload and without noise. The rules:

- Motion means something changed in the garden; nothing moves on its own except the
  elapsed fill and the countdown.
- One easing for everything, `cubic-bezier(.2,.7,.2,1)`; entrances 360ms, exits 500ms,
  in-place changes 200ms, growth 1200ms.
- `prefers-reduced-motion` is honoured by the app's global rule: every transition becomes
  instant, nothing is hidden or delayed.

| moment | what happens |
|---|---|
| a run arrives (`dispatch`) | its strip is inserted at the top of Now at 0 height and 0 opacity, grows to its height and fades in (360ms); its left rule is the accent for 3s, then the hairline. The five-second sentence and the slot count update at the same moment. |
| a run advances | the elapsed fill widens every 15s by CSS transition on `width` (client timer); the last words cross-fade (200ms) when a `progress` message changes them; spend or tokens tick up in place. |
| a run finishes (`run_finished`) | the fill stops and the bar keeps its end mark; the glyph cross-fades to fruit, wilt, tag or pressed (200ms) and the verdict or status appears in bold mono where the spend was (`done · $1.42`, `approve · 4 of 4 criteria`, `failed: exit 1`); the strip holds for 8s, then collapses (height and opacity to 0, 500ms) and leaves. |
| a run fails | as above with the wilt glyph and the **failed** stamp; if the task now has a `needs_human` stop, a needs-you strip arrives in the same movement as a run. |
| a record without a process is reaped | its strip leaves like a finished run, without the 8s hold; the slot count and the sentence update. |
| a merge is held / a harness pauses | a held or paused strip arrives like a run; it leaves like one when the hold clears (`automerge_blocked` gone, `dispatch_resumed` with the harness). |
| the queue moves (`transition`, `review`, `merge_head`, `automerged`, `check`) | Next is re-fetched as a partial; lines that stay move to their new positions by transform (300ms), a line that left fades out, a new one fades in. |
| a task lands (`transition` to `done`) | the plant's clip rises to the new fraction (1200ms) and the newly inked band carries a moss wash that fades over 2s; only after the growth ends does the label's count tick up, so the eye sees growth before the number. The goal's mark changes with the count. |
| a tick (`tick` message) | the countdown resets; the rail's tick line updates; if the tick dispatched nothing and reaped nothing the page does not move. |
| the window changes | a full navigation with `?window=`; not animated. |

## Live updates

One server-sent-events endpoint, `/now1/stream`, off the tick's path and never holding the
hub lock. It carries three message kinds:

- `event`: one events.jsonl line, tailed from the file (the log is the source of history,
  so nothing is invented for the page);
- `progress`: for each active run, every 10s and only when its `stdout.json` mtime moved:
  `{task, run, said, cost_usd, tokens, elapsed_s}`. The reader is `Harness.progress(stdout)`,
  the partial-stream sibling of `Harness.parse`: for `claude` stream-json the newest
  `assistant` event's text block and the sum of each message's `usage`, priced with
  `_usage_cost` (None when the table has no entry for the model, in which case the client
  shows the tokens); for `codex` the newest `agent_message` and the `turn.completed` usage
  priced the same way;
- `tick`: emitted by the `Hub` after each pass: `{at, duration_s, summary, next_at}`. The
  hub already knows both; this is the one message the log does not carry.

What each element listens to:

| element | driven by |
|---|---|
| a Now strip appears | `dispatch` (any mode) |
| a strip's words, spend or tokens, elapsed | `progress` |
| a strip finishes and leaves | `run_finished` |
| held / paused / needs-you strips | `merge_head`, `transition` to `changes_requested` or `waiting_human`, `needs_human`, `harness_paused`, `dispatch_resumed`, `resumed`, `answer` |
| slots and the five-second sentence | `dispatch`, `run_finished`, `dispatch_paused`, `dispatch_resumed` |
| Next, all of it | `dispatch`, `transition`, `review`, `check`, `merge_head`, `automerged`, `rebase`, `pr_opened`, `feedback`, `conflict`, `harness_paused`, `dispatch_resumed` |
| the countdown and tick line | `tick` |
| the plant, its label, the goals | `transition` to `done` (growth), any `transition` of a phase task (label counts), `retro_verdict`, `phase_frozen`, `phase_closed` |
| the last period | `run_finished`, `transition` to `done`, `review`, `automerged`, and the hand-step kinds listed above; the chart is re-fetched at most once a minute |

The client is one small script: it opens the stream, keeps a map of run ids to strips, and
for a list region fetches the server-rendered partial (`/partials/now1/<region>`) rather than
building markup in the browser, so the template stays the one source of markup. There is no
polling loop; a stream that drops reconnects and the page re-fetches all four partials once.

## Data

| element | source | function |
|---|---|---|
| runs in flight | `.garden/runs/*/*/run.json` | `RunStore.active()` less manual runs (the scheduler's `active_runs` rule); `Scheduler.worker_runs_active`, `check_runs_active`, `review_runs_active` for slots |
| a record without a process | the run record | `Run.pid is None` and no `stdout.json` |
| elapsed, typical | run records | `Run.elapsed_minutes`; `now1.typical_seconds(runs)`: median elapsed per (mode, harness or token-free, difficulty) over the last seven days, falling back to (mode, harness or token-free) all-time, at least three samples, counting only runs that reached an outcome (done, failed, timeout, blocked): a cancelled, superseded or env_error run says nothing about how long the work takes, and a mechanical rebase must not share a median with an agent one |
| last words, spend or tokens so far | the run's `stdout.json` | `Harness.progress` (new) |
| held merges, in-review facts | `state.json` per task: `automerge_blocked`, `automerge_candidate`, `automerge_ready_at`, `merge_head`, `checks`, `review_rounds`, `pending_reviews`, `needs_human`, `question` | `State.get`, `inbox.merge_queue_view`, `inbox.needs_human_info` |
| paused harness, dispatch pause | `state.json` `_control` | `Scheduler.control`, `paused_harnesses` |
| dispatch order | task files and state | the queue `dispatch_ready` builds, factored into `Scheduler.dispatch_queue()` (new, returns `[(task, mode, why, skip_reason)]`) so the page and the tick cannot disagree |
| harness and model per line | config tier map | `Scheduler.runner_for`, the harness's `models` |
| next tick | hub | `Hub.last_tick` and `tick_interval` |
| phase, plant, plate, tasks | store | `Store.products`, `Phase`, `plants.plant_info`, `plate()` |
| goals and marks | `goals.md` | `now1.goal_marks(goals_text, tasks)` (new) |
| retro verdict | `state.json` `_retro_verdicts` | `Scheduler.retro_verdict` |
| spend against budget | run records and config | `Scheduler.spent_for`, `budgets` |
| merged, first pass | events | `events.metrics` on the window's events |
| cost, cost by activity, annotations | events and the operator ledger | `costs.cost_series`, `operator_spend.to_cost_events`, `profile_changed` and `config_reloaded` events |
| runs by harness and model | events | `run_finished` grouped by `harness`, `model` |
| hand steps | events | the kinds listed under The last period |
| throughput | events | `run_finished` per bucket via `costs.bucket_key` |

All of it is assembled by `garden/now1.py` into one dict, `now1.snapshot(store, sched,
runs, events, window)`, which the route renders and `garden now1` prints as text. `now1`
reads through the existing store, state, run store and event log only; it makes no network
call and does not import the web package. It is a separate module from Now 2's so the two
builds land in parallel without a conflict; `typical_seconds` and `goal_marks` are the
candidates for a shared `now.py` when one page is retired.

## States

Every state, where it shows, and what a person sees:

| state | Now | Next | Where we are | The last period |
|---|---|---|---|---|
| running | strips as above | the queue | the sheet | the numbers |
| finishing | the strip holds 8s with its verdict, then leaves | the finished task moves to its new line (in review, done) | grows on a merge | merged count ticks up |
| held | a held strip with the reason, stamp **held** | the PR's line says `held: <reason>` | | |
| paused | header stamp **paused** with who, when and why; runs in flight still show; or a paused-harness strip | lines that need that harness say `harness paused` | | |
| failed | the strip with the wilt glyph and stamp **failed**, then a needs-you strip if a stop was recorded | a task sent to `failed` leaves the queue | | |
| no process | a dotted strip at half strength, `no process recorded · started 01:01Z · a slot is held until a tick reaps it`; the slot count says `(n without a process)` | `no slot free` names the cause when it is this | | |
| empty (nothing running, work queued) | one empty mount sheet, tape and a label: `Nothing running. Next tick in 0:42: it will dispatch CG-237 and CG-215 into 5 free slots.` | the queue, unchanged | | |
| quiet (nothing running, nothing to dispatch) | the same sheet: `The garden is quiet. Nothing is ready: 3 drafts wait for approval, 2 cards wait on you in the Inbox. Next tick in 0:42 polls PRs only.` | `Nothing queued. Approve a draft on the Board to add work.` | the sheet, stage word as is | the numbers, still true |
| no open phase | | | the herbarium row and `no open phase: garden new-phase starts the next` | window falls back to `last 24 hours` |
| no runs in the window | | | | `No runs finished in this window.` with the window links |

The empty states are drawn, not blank: the empty mount sheet uses the same `.specimen`
with its tape and a typed label, so a quiet garden still looks like the herbarium.

## Accessibility

- Every glyph has its stage word in a `<title>`; every state has a word beside its colour.
- The stream's changes are announced through one `aria-live="polite"` region that says the
  event in words ("CG-250 finished: approve"), not the strips themselves, so a screen reader
  hears one line per change rather than a re-read region.
- Text contrast follows the app's tokens; the faint colour is used only for secondary words
  that are also available elsewhere (the stage word, "so far").
- The whole page reads in order with no script: the strips, queue, sheet and numbers are
  server-rendered; the script only keeps them current.

## What the build needs beyond the template

Named here so the build makes no design choice:

- `garden/now1.py`: `snapshot`, `typical_seconds`, `goal_marks`, the window resolution
  (sharing `pages.costs.resolve_since`), and a text renderer for `garden now1`.
- `Scheduler.dispatch_queue()` factored out of `dispatch_ready` and used by both.
- `Harness.progress(stdout)` beside `parse`.
- `Hub.tick` publishing the `tick` message, and the stream endpoint with the run watcher.
- The nav entries `Now 1` and `Now 2` first in `base.html`, the `page-now` body class with
  its two rules (the narrow rail, the hidden Running now block), and `garden walkthrough`
  capturing `/now1`.
- The styles in the mock's second `<style>` block, lifted as they are.

## The mock

`docs/design/now-1.html` is rendered by `docs/design/now_1_mock.py` from
`docs/design/now-1-snapshot.json`, a snapshot of this garden taken on 2026-09-06 through the
store, the state file, the run records and the event log (read-only; the script never
imports the scheduler's writers). The snapshot caught the garden mid-trial: the two Now
designs' own runs are among the strips, and the last words on the first strip are this
design's worker reporting its progress. The builder inlines `base.html`'s stylesheet and
`plants.DEFS` so the mock tracks the app's tokens, and adds the Now page's own styles, which
are the ones the build lifts into the template. Open it as a file; add `?theme=dark` to see
the dark palette, `?window=24h` for another window, and the heading's buttons toggle theme
and the states gallery (`?gallery=1`). The gallery at the bottom is not part of the page: it
shows one strip per state and the empty and quiet sheets, so every state is on one screen.

The mock was checked at 1280 and 390 wide, light and dark, in Chrome. Two things it showed
that the page design now accounts for: the Next list needs the title on its own row, or the
harness column squeezes every title into four lines; and this garden has run records
without a process holding worker slots, which is why the page has a state for them.

## Anticipated questions

*Why not a card grid?* Because the four answers are not equal: what is running is the
reason the page exists, and what comes next is second. A grid says "these are the same";
the layout says "read this first".

*Why does the page carry the queue at all when the Board's backlog has it?* The backlog is
every task in a phase in priority order; Next is what the tick will actually take, with
rebase and revise rounds first and the skips named, and it is the one place that order is
shown. It links to the backlog for the rest.

*Why is held work in Now rather than Next?* A held merge is a decision waiting on a
person, and the Inbox is not on the second screen. Anything that needs a hand shows where
the person is looking, in the Inbox's own words.

*Why no red for an overrun?* Colour is for state, and an overrun is not a state: a review
that takes twice its typical time is not failing. The dashed extension and the words say
it plainly.

*Why tokens instead of an estimated price?* An estimate that disagrees with the Costs page
after the run finishes would be a defect. Tokens are what the stream reports; the dollars
appear the moment a price table names the model, and CG-213 and CG-251 bring the tables.

*Why show a run record with no process at all?* Because the scheduler counts it, so "5 of 5
slots, nothing dispatching" would otherwise be unexplained. The page says what is holding
the slot; the fix belongs to the scheduler, and is filed.

*Why does the plant reveal from the ground rather than change stage?* The stage glyphs
are a state vocabulary; the reveal is a measure. The two are used for what each is: the
sheet shows the fraction as height and says the stage word on the label.

*Why is the last-period chart the only coloured thing?* The activity colours are the Costs
page's; two pages showing the same figure in different colours would be a defect.

*Why is the rail's Running now hidden here and nowhere else?* The rail block is the short
form of this page's first region; showing both at 1280 is the same list twice, side by
side. Every other page keeps it because there it is the only view of the runs.

*What does a first-time visitor do next?* Read the sentence, then click any id. The page
tells them where the person's hand is needed and nothing else asks for one.
