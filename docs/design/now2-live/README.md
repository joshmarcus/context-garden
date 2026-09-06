# Now 2, live implementation

`/now2` implements the Astra composition from `../now-2.md`: a broad list of
active work beside independent dispatch, merge and review queues; phase specimens
and windowed outcomes below. `garden now --page 2` reads the same model.

The design's horizontal navigation applies only on this page. All active runs
remain in the document, including auxiliary and concurrent runs for one task.
The implementation uses the design's metric picker and persistent, independently
scrolling comparison table. Its compact throughput bars show twelve acceptance
buckets with numbered annotation rules. Exact counts accompany the growth glyph;
phase artwork identifies the specimen rather than implying percentage completion.

The baseline has a JSON `/api/events` endpoint, not an SSE endpoint. The new
`/api/events/now2` adapter observes the existing event log, run/output records,
task/goals files and operator ledger outside the controller. It pushes keyed,
escaped HTML fragments. Browser updates retain disclosure/focus state, use one
server clock anchor, and make no polling requests. Reconnection sends an
authoritative snapshot. A finished run dwells for eight seconds; focused or
expanded rows remain until released. Reduced motion and the Motion off control
suppress movement without stopping clocks.

Matrices come from `events.metrics` / `outcomes.difficulty_by_model`, including
full lifecycle cost for accepted tasks, explicit missing-price counts, reviewed
first-pass denominators, and unrounded row-relative ranks. `garden metrics`
prints the same five matrices; `--since` / `--until` bound those comparisons.
Legacy summary tables retain their established lifetime semantics.

Unavailable facts stay explicit: unmapped goal progress, insufficient duration
samples, unreported spend, missing manual-merge attribution, unknown next tick,
and operator ledger entries without phase attribution. In the phase window,
unattributed operator spend is shown separately with its whole-garden share,
rather than allocated to that phase. Existing run-detail clocks are unchanged;
Now 2 computes from the same authoritative run start and finish timestamps.

## Captures

The four full-page PNGs are a synthetic, populated fixture served by this
worktree's live FastAPI app with `watch=False`, never a controller run:

- [1280 light](light-1280-full.png)
- [1280 dark](dark-1280-full.png)
- [390 light](light-390-full.png)
- [390 dark](dark-390-full.png)

Windows Edge is launched with the product overview's headless flags. The capture
script sets exact CSS viewport dimensions and light/dark media through its
DevTools protocol, then captures the full document and inspection strips. This
uses the same Edge rendering engine as the command-line screenshot recipe and
supports the entire phone document. Scanned plates are existing repository assets.

The inspected captures show wrapped metadata and attention reasons, visible
elapsed clocks, independent queue headings, a phase plate and goals, green/red
comparison grounds with n and rank symbols, throughput annotations and operator
spend. The phone document remains 390 pixels wide; only the model comparison
scrolls. Inspection prompted combining a held reason with its existing task
attention item and containing the table's accessible labels to remove a
13-pixel document overflow. A further inspection exposed collapsed phone table
columns; explicit cell widths now keep difficulty labels readable while the
model columns scroll. Final document widths measured exactly 1280 and 390 pixels
in both themes. All four final documents were inspected, including every phone
section in full-resolution strips.

## Reproduce

From this worktree, run:

```sh
PYTHONPATH=src .venv/bin/python docs/design/now2-live/serve_fixture.py
```

The fixture lives in `.pytest_cache/now2-live-fixture` and listens at 8769. It
writes synthetic records only, and never dispatches, pushes, or calls GitHub.
Launch a dedicated Edge profile with `--headless=new --disable-gpu
--hide-scrollbars --remote-debugging-port=9349` and a Windows-side
`--user-data-dir`. Copy `capture-edge.ps1`, `browser-checks.ps1`, and
`browser-checks.js` to `C:\Users\joshm\AppData\Local\Temp\cg309-captures`.
Run the PowerShell scripts sequentially, then copy the four `*-full.png` files
back here. The browser checks exercise clock boundaries, server offset,
missing/overdue typical duration, every picker option, focus/disclosure retention,
phone table scrolling, motion off and absence of polling requests.

The focused tests live in `tests/test_now2.py` and `tests/test_now2_metrics.py`.
On a host with a large shared pytest temporary tree, use an isolated
`--basetemp=.pytest_cache/cg309-tests` to avoid unrelated directory cleanup.
