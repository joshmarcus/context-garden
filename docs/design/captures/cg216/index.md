# Walkthrough of the live web app — demo/p1, 2026-09-07

Each page below has its purpose, one line on what to look at, a full-page screenshot, the served HTML and a plain-text rendering (tags stripped, in document order) that reads roughly as the page does top to bottom.

Read the `.txt` for the words and the order; read the `.html` for structure, controls, forms, empty states and error text.

Run page stderr is omitted (rerun `garden walkthrough` with --include-stderr to capture it); absolute home-directory paths are redacted to `~` throughout.

## Now 2: `/now2` (HTTP 200, 87 KB)

Live work, dispatch and merge queues, phase progress and windowed outcomes.

Look at: Can you see what is running and what that work adds up to?

![Now 2, 1280px, light](now2-1280-light.png)

![Now 2, 1280px, dark](now2-1280-dark.png)

![Now 2, 390px, light](now2-390-light.png)

![Now 2, 390px, dark](now2-390-dark.png)

Files: `now2-1280-light.png`, `now2-1280-dark.png`, `now2-390-light.png`, `now2-390-dark.png`, `now2.txt`, `now2.html`

## Now: `/` (HTTP 200, 67 KB)

The first page: everything that needs the operator now.

Look at: Can a person immediately tell what needs action?

![Now, 1280px, light](now-1280-light.png)

![Now, 1280px, dark](now-1280-dark.png)

![Now, 390px, light](now-390-light.png)

![Now, 390px, dark](now-390-dark.png)

Files: `now-1280-light.png`, `now-1280-dark.png`, `now-390-light.png`, `now-390-dark.png`, `now.txt`, `now.html`

## Now 1: `/now1` (HTTP 200, 94 KB)

What is running, what is next, where the phase is and the last period, live from the events stream.

Look at: Can you say what the garden is doing and what comes next within five seconds?

![Now 1, 1280px, light](now1-1280-light.png)

![Now 1, 1280px, dark](now1-1280-dark.png)

![Now 1, 390px, light](now1-390-light.png)

![Now 1, 390px, dark](now1-390-dark.png)

Files: `now1-1280-light.png`, `now1-1280-dark.png`, `now1-390-light.png`, `now1-390-dark.png`, `now1.txt`, `now1.html`

## Inbox: `/inbox` (HTTP 200, 67 KB)

What needs a decision and what is only a notice; the rail badge counts decisions only.

Look at: Is the split between a decision and a notice clear, and is the empty state designed?

![Inbox, 1280px, light](inbox-1280-light.png)

![Inbox, 1280px, dark](inbox-1280-dark.png)

![Inbox, 390px, light](inbox-390-light.png)

![Inbox, 390px, dark](inbox-390-dark.png)

Files: `inbox-1280-light.png`, `inbox-1280-dark.png`, `inbox-390-light.png`, `inbox-390-dark.png`, `inbox.txt`, `inbox.html`

## Board (columns): `/board` (HTTP 200, 73 KB)

The board in columns, one per status in the loop's order.

Look at: Do the columns read left to right as the loop moves work?

![Board (columns), 1280px, light](board-1280-light.png)

![Board (columns), 1280px, dark](board-1280-dark.png)

![Board (columns), 390px, light](board-390-light.png)

![Board (columns), 390px, dark](board-390-dark.png)

Files: `board-1280-light.png`, `board-1280-dark.png`, `board-390-light.png`, `board-390-dark.png`, `board.txt`, `board.html`

## Board (list): `/board?view=list` (HTTP 200, 73 KB)

The board as a list grouped by status, with a per-state fact on each row.

Look at: Does each row say enough to act without opening the task?

![Board (list), 1280px, light](board-list-1280-light.png)

![Board (list), 1280px, dark](board-list-1280-dark.png)

![Board (list), 390px, light](board-list-390-light.png)

![Board (list), 390px, dark](board-list-390-dark.png)

Files: `board-list-1280-light.png`, `board-list-1280-dark.png`, `board-list-390-light.png`, `board-list-390-dark.png`, `board-list.txt`, `board-list.html`

## Trellis: `/trellis` (HTTP 200, 71 KB)

The dependency and stacking graph with growth-stage glyphs and the hide-done control.

Look at: Can you follow what blocks what, and what the glyphs mean?

![Trellis, 1280px, light](trellis-1280-light.png)

![Trellis, 1280px, dark](trellis-1280-dark.png)

![Trellis, 390px, light](trellis-390-light.png)

![Trellis, 390px, dark](trellis-390-dark.png)

Files: `trellis-1280-light.png`, `trellis-1280-dark.png`, `trellis-390-light.png`, `trellis-390-dark.png`, `trellis.txt`, `trellis.html`

## Phase: `/phases/demo/p1` (HTTP 200, 73 KB)

The phase page: goals, the task table, budget and cost, persona reviews.

Look at: Is the important thing (what needs you) above the fold?

![Phase, 1280px, light](phase-1280-light.png)

![Phase, 1280px, dark](phase-1280-dark.png)

![Phase, 390px, light](phase-390-light.png)

![Phase, 390px, dark](phase-390-dark.png)

Files: `phase-1280-light.png`, `phase-1280-dark.png`, `phase-390-light.png`, `phase-390-dark.png`, `phase.txt`, `phase.html`

## Task: `/tasks/DM-001` (HTTP 200, 71 KB)

A task page: state, tier and priority controls, runs, the live log, the actions.

Look at: Are the controls and the run history legible, and is it clear what happens next?

![Task, 1280px, light](task-1280-light.png)

![Task, 1280px, dark](task-1280-dark.png)

![Task, 390px, light](task-390-light.png)

![Task, 390px, dark](task-390-dark.png)

Files: `task-1280-light.png`, `task-1280-dark.png`, `task-390-light.png`, `task-390-dark.png`, `task.txt`, `task.html`

## Runs: `/runs` (HTTP 200, 66 KB)

Every run with its cost and tokens.

Look at: Is cost easy to total and attribute?

![Runs, 1280px, light](runs-1280-light.png)

![Runs, 1280px, dark](runs-1280-dark.png)

![Runs, 390px, light](runs-390-light.png)

![Runs, 390px, dark](runs-390-dark.png)

Files: `runs-1280-light.png`, `runs-1280-dark.png`, `runs-390-light.png`, `runs-390-dark.png`, `runs.txt`, `runs.html`

## Herbarium: `/herbarium` (HTTP 200, 65 KB)

A plate per phase; closed phases live here.

Look at: Does a closed phase read as a finished, catalogued thing?

![Herbarium, 1280px, light](herbarium-1280-light.png)

![Herbarium, 1280px, dark](herbarium-1280-dark.png)

![Herbarium, 390px, light](herbarium-390-light.png)

![Herbarium, 390px, dark](herbarium-390-dark.png)

Files: `herbarium-1280-light.png`, `herbarium-1280-dark.png`, `herbarium-390-light.png`, `herbarium-390-dark.png`, `herbarium.txt`, `herbarium.html`

## Config: `/config` (HTTP 200, 74 KB)

Configuration: pause and resume, live overrides, the tier map.

Look at: Are the live controls and their effect clear?

![Config, 1280px, light](config-1280-light.png)

![Config, 1280px, dark](config-1280-dark.png)

![Config, 390px, light](config-390-light.png)

![Config, 390px, dark](config-390-dark.png)

Files: `config-1280-light.png`, `config-1280-dark.png`, `config-390-light.png`, `config-390-dark.png`, `config.txt`, `config.html`

## Trials: `/trials` (HTTP 200, 66 KB)

The model leaderboard from every trial.

Look at: Does the ranking say which model to pick and why?

![Trials, 1280px, light](trials-1280-light.png)

![Trials, 1280px, dark](trials-1280-dark.png)

![Trials, 390px, light](trials-390-light.png)

![Trials, 390px, dark](trials-390-dark.png)

Files: `trials-1280-light.png`, `trials-1280-dark.png`, `trials-390-light.png`, `trials-390-dark.png`, `trials.txt`, `trials.html`

## Events: `/events` (HTTP 200, 66 KB)

The event timeline.

Look at: Can you reconstruct what happened from the timeline alone?

![Events, 1280px, light](events-1280-light.png)

![Events, 1280px, dark](events-1280-dark.png)

![Events, 390px, light](events-390-light.png)

![Events, 390px, dark](events-390-dark.png)

Files: `events-1280-light.png`, `events-1280-dark.png`, `events-390-light.png`, `events-390-dark.png`, `events.txt`, `events.html`
