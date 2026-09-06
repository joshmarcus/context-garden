# Now 2 — work in view

The page makes the work legible before it makes the machinery legible. A broad,
ruled list shows the garden working; a narrower list names what can move next.
Below, a mounted phase specimen and a small set of outcome comparisons explain
what that movement adds up to. The reading order is **Now → Next → Where we are
→ The last period**. Nothing on this page changes garden state.

This is the design target for `/now2`; its navigation label is **Now 2**, beside
**Now 1**. It does not choose the eventual `/now`. No Now 1 artifacts informed this
design. The owner explicitly requested this design despite phase 05's general
non-goal of new UI; implementation remains the dependent build's scope.

## Artifact and provenance

Open `../../src/garden/web/static/mock/now-2.html` from this document's directory,
or open the file directly. It needs no server, package installation or network.
The app currently mounts only the plates directory, so this is a file preview,
not an assertion that `/static/mock/now-2.html` is a served route.

**The mock is a labelled simulation, not yet a real-state acceptance artifact.**
Phase identity, plant and goal wording come from the actual phase-05 goals;
task titles come from the supplied brief. Counts, queue membership, run times,
transcript excerpts and matrix values are illustrative. No controller state or
event export was supplied to this worktree. Unknown data must not be represented
as an empty garden or zero cost. A timestamped, sanitized snapshot is required to
replace the simulation before the real-state criterion can be claimed.

The external reading files were read through GitHub, not another checkout:

| Source in `joshmarcus/garden` | Read blob SHA |
|---|---|
| `context-garden/product.md` | `ced451f332bbd457c7cfc5e8ce87f3cd74d02fb8` |
| `context-garden/phase-05/goals.md` | `905c49714517be40365261bf9db8e337ba56657f` |
| `context-garden/phase-05/specs/now-page.md` | `1ebb67987f03c21e64a719ee9dce1defa3538323` |
| `context-garden/phase-01-bootstrap/specs/botanical-theme.md` | `a6aa23934a83fdb40ae641aaad078549b9d3e21c` |

The product source baseline is `b240f29`. The mock reuses its plant symbols and
local Thomé plates; credits remain in `static/plates/SOURCES.md`. Rebuild it with
`PYTHONPATH=src .venv/bin/python docs/design/now-2/render.py`.

## Layout and visual system

At 1280px: a 64px horizontal navigation, 40px page margins, 1200px available width.
The upper section is 2:1 (Now / Next), separated by a 32px gutter and a fine rule.
The lower section is 1:2 (Where we are / The last period). The wider list and the
largest heading belong to Now. The phase plate is 150px tall, subordinate to run
titles; it must never push the active work below the initial viewport. The page
grows vertically for all active runs: no carousel, rotation, internal run scroll
or hidden active workers. Ordinary page scrolling is preferable to losing one.

At widths below 800px: the four regions stack in DOM order, 20px page margins
(350px content at 390). Navigation wraps. Run metadata wraps independently of
titles; the transcript is one line with an explicit disclosure for the full text.
Queue reasons wrap without truncation. The phase specimen sits next to its label,
then goals span the width. The metric table keeps the easy/medium/hard rows and
model columns: scroll only its named, keyboard-focusable container, with a sticky
row header and a visible “Scroll to compare models” instruction. Do not turn it
into cards; comparisons need aligned numbers. At 200% zoom the same breakpoint
and wrapping apply. Touch targets are at least 44px.

Use the existing Newsreader / Courier Prime font stacks (Georgia / Courier as
offline fallbacks). Title 44/48px, region headings 26/30px, run titles 22/27px,
body 17/24px, metadata 13/19px, table values 18/24px. Phone title 36/40px. Numeric
text is tabular. Paper surfaces have 1px rules and 2px corners, no raised tiles.
Black ink and whitespace carry the hierarchy; no model or harness brand colours.

| Token | Light | Dark | Meaning |
|---|---|---|---|
| ground | `#f2f5f0` | `#121614` | page |
| surface | `#ffffff` | `#1a201c` | paper |
| ink | `#17201b` | `#e6ece7` | all meaningful text |
| muted | `#5f6b64` | `#98a59e` | supporting text |
| movement | `#2b7a57` | `#5cc493` | elapsed fill, newly grown stem |
| running / review | existing `--s-running` / `--s-in_review` | existing theme equivalents | small state marks only |
| held / paused / failed | existing warn / warn / bad | existing equivalents | word and symbol plus colour |
| best ground | `#dcefe4` | `#234334` | favourable relative outcome |
| worst ground | `#f5e1df` | `#492d2d` | unfavourable relative outcome |

The plate keeps its archival paper mount in both themes. Its colours identify
the specimen; they do not encode dashboard categories. Growth glyphs reuse
`plants.DEFS`, `stage_svg`, `plant_svg`. Scanned plates are phase identity, while
the small glyph and growing stem are phase progress. This avoids pretending a
fully flowering historical drawing is a measurement of current completion.

## Now

Heading: “Now”, then a literal summary: “3 runs in flight · 1 needs you”. A run
row contains, in order: state glyph and plain mode; linked title; harness, exact
model, tier; latest assistant sentence; live elapsed clock and typical-duration
mark; known spend with its update time. Mode vocabulary includes work, revise,
resume, review, rebase, check, persona, edit, retro, kickoff, trial and compare.
Check runs with no model say “local check”. Auxiliary runs use a descriptive title
and phase link when there is no task. Stable identity is `(task_id, run_id)`, never
task alone: a trial or review may coexist with another run for the same task.

Task IDs are available in the link's accessible description and disclosure, not
the prominent copy. This follows phase 05's explicit no-IDs-in-copy instruction
where the older spec's initial content list asks for an ID. Links go to the
existing task/run detail surfaces; every title remains identifiable in a demo.

Do not infer progress from elapsed time, token count or a worker saying “done”.
Stage changes use authoritative run/task facts. The typical mark is a duration
comparison, not a percent-complete bar. Live spend is “— · not reported yet” until
the harness records usage/cost; a completed run with a missing price remains
unknown. Assistant text is escaped plain text, capped at 180 characters for the
summary, with a keyboard/touch disclosure and a link to the full transcript.

Attention is a ruled band directly below the Now heading, before run rows:
held merge + literal `automerge_blocked` reason; paused harness + name/reason;
needs-you item + question/stop and Inbox link. Reuse `build_inbox`/`decisions` and
`needs_human_info`, deduplicating a held reason already represented by that item.
Show all attention reasons without treating “longer than usual” as an alert.
Runs continue showing when dispatch is paused: dispatch pause is not run pause.

| State | Rendering and persistence | Why |
|---|---|---|
| running | clock, latest text, mode and typical mark | enough to distinguish work from inactivity |
| finishing | freeze at `finished_at`; “Work finished · checks next”, actual verdict when known | run completion is not a merge |
| held | stationary amber rule, “Merge held”, full reason, task link | makes the impediment explicit |
| paused | “Dispatch paused” or “[harness] paused”, scope and reason; active clocks continue | avoids implying workers stopped |
| failed / timeout | wilt glyph, failure word and reason; unresolved item persists | a failure must not silently fade |
| empty | “No runs yet”; setup/approval reason if known, phase and Inbox links | honest first-use state |
| quiet | “Nothing running”; actual wait reason and next tick time | distinguishes a healthy wait from missing data |
| unavailable / disconnected | keep last snapshot, “Updates disconnected · last received …”; clocks labelled “elapsed since start” | freshness is distinct from workload |

Quiet reason priority: dispatch paused, harness unavailable, needs-you gate,
dependencies blocked, ready and waiting for next tick, then no approved work.
Show additional reasons in Next. `next_tick_at` is the scheduler's scheduled next
pass, not `last_tick + interval` while a pass is still running. If unknown show
“Next tick not reported”; with watch off say “Scheduler off”. Never count below
zero or imply an overdue tick has happened.

## Next

Three labelled lists: **Workers**, **Merges**, **Reviews**. Each has independent
capacity and ordering; there is no fabricated global queue across all three.
Workers list eligible rebase work, eligible revisions, then the ready set, exactly
as `DispatchMixin.dispatch_ready` chooses. Within a group preserve scheduler
ordering, including the existing iteration order for rebase/revise; do not apply
an attractive but different priority sort. Each item shows its mode and its
specific reason (“revision before new work”, “waiting for a worker”, “blocked by
Onboarding”, “harness paused”). Skipped candidates belong in a separate “Waiting”
sublist without numbered eligible positions. Show all items in a disclosure after
the first three per queue, preserving order and total.

Merges put the current `merge_head` first, then candidates ordered by
`automerge_ready_at` and the scheduler's tie-break; show review round, CI and
rebase state from side-store facts. Held PRs removed from the queue remain in
Now's attention band and the Waiting list. Reviews use the actual pending-review
drain order and review-slot limit, not worker capacity. No ETAs are guessed.

Extract a pure scheduler report/selection helper in the build, shared with
dispatch; calling `dispatch_ready`, `budget_exceeded` (which can emit) or queue
mutators to render a preview is forbidden. Report eligibility and reason from
copied state/config. An empty queue explicitly says “No eligible …”; missing
state says “Queue unavailable”.

## Where we are

Each open phase has a specimen label: product, phase, plate, Latin name, done /
total and any retro verdict. All open phases render in store order; never pick
one by last event. The denominator is all phase tasks, matching `phase_summary`;
also disclose cancelled and won't-do counts so the remaining work is unambiguous.
`done` is terminal completion, not PR opened or merged into a parent branch.

A thin stem beside the plate has six small, discrete stage stations: seed at
0 done; sprout above 0 and below 20%; leaf at 20%; bud at 50%; flower at 80%;
fruit only when all tasks are terminal and the phase is closed. A phase with all
work done but no close verdict remains flower, labelled “Awaiting phase close”.
Zero total shows seed and “No tasks yet”, never 100%. Reopened phases can regress;
update immediately with the text “Scope changed”, no celebratory reverse motion.
The plant is a mnemonic; the adjacent exact counts carry the evidence.

Goals list the actual goal headings with ✓ merged, → in flight, ○ not started.
These need explicit goal-to-task membership, not keyword matching or LLM inference.
If no mapping exists show “Progress not mapped” (the mock includes this state).
For a mapped goal, all mapped tasks done → merged; any dispatched/nonterminal
work → in flight; otherwise not started. Empty mapping is unknown, not complete.
Show a retro verdict verbatim with pending/accepted status. Below the open phases,
closed phases are small linked specimens in a wrapping row, newest first, with
their plate and phase labels. Never imply an open phase has been pressed.

## The last period

Default window: **last hour**, with **today (UTC)**, **last 24 hours**, **this phase**.
With multiple phases, “this phase” requires the phase selector (default first open
phase in store order, visibly named). Windows are `[since, as_of)` using server
UTC, not each browser's timezone. Display both boundary times. Window changes
refresh only this region; Now, Next and phase counts remain current and unfiltered.

Start with a sentence of outcomes: tasks merged, first-pass approval and total
recorded spend. Under it place a server-rendered step sparkline of accepted tasks
in 12 equal-duration buckets; label first/last time and total, provide an adjacent
text disclosure with bucket values. Zero activity is a flat zero line; unknown
history is a text absence, never a zero line. Event annotations are thin vertical
rules with numbered, keyboard-accessible captions below: operating-profile/config
changes and successful pins (`upgraded`), including those in zero-spend buckets.

Then a single **metric picker** and the difficulty-by-model matrix. Default is
**Total cost / accepted task**, since cheap individual runs can hide costly
revision loops. Picker options: total cost / accepted task; mean work-run cost;
first-pass approval; mean revise rounds; median lead time. Caption states unit,
direction, cohort, n meaning and missing-price count. No comparison is described
as statistically significant or a model recommendation.

The build must extend the shared `events.metrics` computation and consume its
result for both the CLI and Now; neither Jinja nor browser JS aggregates metrics.
The current `metrics()` has independent difficulty/model breakdowns, lifetime
event semantics, an average lead time, and no joint matrix. The Costs route
currently does not apply its window to `outcomes`. Do not mistake those fields
for the requested contract or fix unrelated Costs routing in this design task.

### Exact computation contract for the build

Keep full history through `as_of` to identify first dispatch/review and all
supporting run costs; do not truncate history before finding task lifecycles.
The matrix has rows easy, medium, hard, and a sorted column for every model with a
work/revise/resume run overlapping the window, plus models attributed to accepted
tasks in that window. Unknown models get an “unknown model” column. A column with
no applicable observations stays present with `— · n=0`.

Task metrics use the cohort of unique tasks observed accepted into the product
base in the window. Keep current `metrics` route attribution: a task belongs to
each implementation model it used (work/revise/resume), and its complete task
cost, including checks/review/rebase/edit, belongs to each such column. Say below
the matrix: “A task using several models appears in each. Columns do not add up.”
Never attribute that cost only to its final reviewer. Rows use the task difficulty
as in `metrics`; run-duration comparisons use the run's dispatched tier.

| Element / proposed shared result field | Numerator / definition | Denominator / n |
|---|---|---|
| `accepted_count`, throughput | unique base-accepted task IDs with accepted time in window | count, never PRs opened or all done transitions blindly |
| `matrix.total_cost` | mean complete lifetime cost through acceptance for cohort members with all run costs known | accepted tasks with complete prices; separately expose accepted total and incomplete-cost count |
| `matrix.work_cost` | mean `cost_usd` of completed `mode=work` runs finishing in window, matching row and exact run model | priced work runs; expose unpriced count; exclude review/revise/check |
| `matrix.first_pass` | cohort members whose first authoritative review verdict is approve | reviewed accepted tasks; no review is missing, not failed |
| `matrix.revise_rounds` | mean number of `dispatch(mode=revise)` in each accepted task's lifecycle | accepted tasks with dispatch history; preserve existing revision semantics |
| `matrix.lead_time` | median seconds, first dispatch → base acceptance | accepted tasks with both timestamps; missing/negative duration excluded and disclosed |
| summary first-pass | same first-pass calculation across unique accepted tasks | reviewed accepted tasks, not summed model cells |
| activity spend | `cost_series` over `run_finished` in the window plus operator ledger converted with `to_cost_events` | recorded dollars; missing prices separately disclosed |
| runs by harness/model | distinct completed run identities in the window, grouped by recorded harness/model | runs (all modes, including auxiliary runs) |
| hand steps | recorded human actions in the window, with a separate manual-merge count when attribution exists | unknown if audit events lack source; not inferred from missing automerge events |
| typical duration | median `finished_at - started_at` for successful, completed runs in window, same mode and run tier | at least 3 runs; otherwise “Typical not established (n=…)”; no model pooling beyond mode/tier rule |

Expose each matrix cell as `{value, n, missing, unit, direction, rank, shade}`.
Return null for missing/zero denominator, never zero. Precision: USD two decimals
(positive values below $0.01 read “<$0.01”), percent whole points, rounds one
decimal, lead time seconds/minutes/hours with full duration in the disclosure.
Rank on unrounded values; expose unrounded values in accessible detail when two
rounded values look tied. A re-open/second acceptance must not duplicate a task
within the same window. The shared metrics extension owns acceptance provenance;
it must distinguish a manually marked-done task without observed base merge.

Activity spend uses directly labelled horizontal bars with neutral hatch patterns;
no categorical rainbow. Run counts and hand steps use definition lists, not a
second numeric comparison table with incompatible units.

### Cell shading

Within each difficulty row and selected metric, let `lo` / `hi` be the smallest /
largest non-null raw values. Lower-is-better: `quality=(hi-value)/(hi-lo)`;
higher-is-better (first-pass only): `(value-lo)/(hi-lo)`. Interpolate worst ground
→ neutral surface → best ground. Use constant ink foreground, not cell opacity.
For `n<3`, mix the resulting ground at 25% strength with the surface and append
`†`; keep n and text at full contrast. Best gets `↑`, worst `↓`, with accessible
labels “best in row” / “worst in row”. These mean favourability, not numeric
direction. Low-n extrema retain their mark plus dagger, labelled provisional.
If all values tie or only one value exists, use neutral ground and `=` (no best /
worst claim). Null cells are unshaded `— · n=0`; zero is a valid ranked value.
Every numeric comparison table uses this rule. No heat-map on heterogeneous
metadata tables or count summaries. Legend is always visible, never hover-only.

## Motion, clocks and event contracts

The page owns one clock anchor: server UTC at receipt plus `performance.now()`
delta. Establish offset once per page through a timestamp response (midpoint of
request/response, avoiding a slow HTML download skew). Clocks calculate
`max(0, floor((server_now-started_at)/1000))` each second; never increment a stored
counter. Formatting: `0s`…`59s`, `1:00`…`59:59`, `1:00:00` onward. On tab return,
recalculate immediately. A future/malformed start shows “Clock unavailable” or
zero with a clock-skew note; do not invent a start. The build shares this clock
with Board, task and run detail to ensure agreement.

The card renders `data-run-id`, `data-started-at` (ISO UTC), optional
`data-finished-at`, `data-typical-seconds`. The document carries
`data-server-now`. The elapsed fill is `min(elapsed/typical, 1)`; it eases for
200ms and is labelled “typical 8:00 · n=12”. Past that mark it stays full and
says **longer than usual**, in ordinary muted ink. Nothing pulses or turns red.
The clock has no `aria-live`: announce run changes, not every passing second.

| Transition | Timing and movement | Meaning / persistence |
|---|---|---|
| run arrives | 240ms ease-out from 12px below, opacity 0→1 | append in stable start order; actual new run counts from zero; a delayed event shows true elapsed, never resets history |
| new assistant line | replace text in place; 120ms ink transition | no typing animation or layout jump |
| run advances mode | settle old run, add new run identity; same task title | a new review clock must not inherit work's start time |
| run finishes | freeze clock; verdict crossfade 180ms; retain 8s | “work finished”, “review approved”, etc., not automatic “merged” |
| successful finish leaves | 240ms opacity fade, then collapse space over 180ms | preserve finished summary in recent activity; no disappearing verdict |
| failure / hold / pause arrives | 180ms rule and label appearance, no slide through a pipeline | item remains until state resolves |
| attention resolves | 180ms label replacement, retain “Resolved” 4s then fade | no confetti |
| merge lands | exact count changes once; stem extends 600ms; station glyph crossfades 240ms if threshold crossed | only authoritative base merge grows the plant |
| quiet → working | replace quiet text with the arriving card in 240ms | no background breathing animation |
| burst / reconnect | coalesce 250ms; render latest state without replay | animation cannot become a backlog |

Under reduced motion or the page's “Motion off” preference, replace all animated
transforms/fades with immediate updates. Retain the eight-second result dwell,
since it conveys information. Clocks still tick. Pause automatic removal while
focus or an expanded disclosure is inside the row; move focus only on explicit
navigation. One polite status region announces coalesced arrivals/completions.
Offscreen rows do not animate. Preserve scroll anchoring and focused DOM nodes.

### Live invalidation map

Events invalidate a read model; they are not complete state patches. Refresh
only affected keyed rows/regions. A reconnect always loads one authoritative
snapshot before accepting deltas. Out-of-order/duplicate events are ignored via
a monotonically increasing stream cursor, not timestamp alone.

| Live element | Existing event kinds that invalidate it | Authoritative read |
|---|---|---|
| Now membership, state and verdict | `dispatch`, `run_finished`, `transition`, `review` | `RunStore.active`, run records, `_aux`, task and review state |
| attention band | `waiting_human`, `needs_human`, `decision`, `stall`, `budget`, `harness_paused`, `dispatch_paused`, `dispatch_resumed`, `retro_verdict`, `retro_question` | shared inbox predicate, control/harness/budget records |
| worker / review ordering | above plus `feedback`, `triaged`, `stacked`, `restacked`, `config_reloaded`, `config_override`, `config_override_cleared`, `profile_changed`, `budget_set` | pure scheduler queue projection and current capacities |
| merge ordering / CI reason | `merge_head`, `review`, `transition`, `pr_closed`, `pr_reopened`, `retargeted`, `ci_rerun` | queue state, `checks`, `review_rounds`, `rebase_pending`, `merge_head`, `automerge_blocked` |
| phase plant/counts/goals | `transition`, `discovered`, `phase_closed`, `retro_verdict`, `retro_done` | phase tree, tasks and explicit goal membership |
| outcome matrix / charts | `run_finished`, base-merge `transition`, `review`, `profile_changed`, `config_reloaded`, `upgraded` | shared windowed metrics, costs, operator ledger |
| elapsed digits/fill | no event needed | browser clock anchor and run timestamps |

**Transport work required:** at this baseline `/api/events` returns the last 50
hub log messages as JSON; it is not SSE despite the spec's assumption. Some queue
changes emit only a log line, and latest assistant text/live usage, goal file
edits and operator-ledger appends have no domain event. The build adds a read-side
SSE adapter with `snapshot`, `invalidate`, `run_output`, and heartbeat messages.
The adapter observes file/version changes outside the tick critical path and
sends invalidations even when no domain event exists. It tails bounded new bytes
of transcripts rather than rescanning full files per card. It does not write to
the scheduler event log to animate a page.

Heartbeat interval 15s, disconnected indication after 45s without a message.
Window boundaries advance on a minute boundary via one read-side invalidation;
no full-page polling. Fetches coalesce by region, carry a revision token, and
discard responses older than the last applied revision. Preserve selections,
expanded disclosures and scroll position. Serve pre-rendered HTML/SVG fragments
with escaped fields; no worker-provided HTML. Snapshot construction takes copied
records, never holds `hub.lock` while rendering, and performs no scheduler action.
Stale/out-of-sequence snapshots trigger another read rather than a guessed state.

## State mock and validation

The file preview includes a primary operational composition and a permanent
state atlas: running, finishing, held, paused, failed, empty, quiet, missing data,
and disconnected. Its controls demonstrate theme, metric switching, motion off,
and an arriving clock. All five matrices exist in rendered HTML (all remain
readable with JavaScript off). Simulation labels remain visible in screenshots.

The render test provides a fixed start timestamp to the card macro and asserts
the exact `data-started-at` read by the clock; it also verifies the completed
timestamp and literal escaping. This proves the mock contract, not the future
live `/now2` route. The build must add its own route and browser clock tests,
including 59→60, 3599→3600, offset clock, new run identity, delayed dispatch,
tab return, missing typical, failure persistence and reduced motion.

Before acceptance, capture 1280px and 390px in light and dark with a real snapshot;
check layout, table scrolling, focus and untruncated queue reasons. Browser tooling
is not installed in the prepared worker environment, so a visual pass cannot be
claimed from markup inspection. The mock uses existing local plates and fallback
fonts so the capture can run without external resources.

Designer and usability-expert persona findings belong here with severity, concrete
change and artifact reference. **Neither review has run:** worker rules prohibit
`garden` commands and the runner has not yet opened this PR. A self-assessment is
not a persona verdict. The runner must perform the requested reviews and return
findings before this criterion is claimed. In particular, validate the five-second
reading test, low-n legend, mobile matrix and distinction between finished/merged.

## Snapshot handoff

Provide inside this worktree a sanitized export with `captured_at`, product/phase
metadata and goals, task frontmatter (no task bodies needed except titles), active
and historical run metadata, current queue/control/attention facts, first/latest
tick and next scheduled tick, and events with enough preceding history to compute
the selected windows. Include explicit completeness flags and model price coverage.
Do not include credentials, full transcripts, host paths or session secrets;
one sanitized latest assistant line per run is sufficient. The renderer can then
replace its simulation inputs without guessing operational facts.
