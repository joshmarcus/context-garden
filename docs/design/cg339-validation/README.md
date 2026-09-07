# Latest operator validation

The operator repaired synchronous replay startup under fast-forward. At b77f4b9,
review admission publishes a detached check and returns; the normal check supervisor
provides the scrubbed environment, heavy-work lease and resource controls. A later tick
collects the manifest digest before starting the model reviewer. The check verifies its
actual checkout SHA. The restart regression proves a new scheduler collects the saved check
without a duplicate replay or prematurely spending a model-review round.

94 focused review/preflight tests passed (146 MiB peak/no swap). The strengthened restart
case passed separately (64.8 MiB peak/no swap). The real served disposable journey passed
all 9 flows / 102 HTTP requests in 4.995 seconds, at 116 MiB peak/no swap under CPU200%,
MemoryHigh512MiB/Max1GiB and a180-second runtime ceiling. It exercised b77f4b9; the subsequent
commit only records these artifacts. `b77f4b9-manifest.json` and `b77f4b9-output.txt` retain
this evidence. Original raw responses remain at the artifact directory in that manifest.

Reproduce from the tested checkout with `PYTHONPATH=src python -m garden.interaction_replay
--out <disposable-directory> --head $(git rev-parse HEAD) --nonce <unique-nonce>` inside a
bounded unit. This is a controlled HTTP journey, not visual layout validation or real-model
load. Broad applicability and generic flow selection remain narrowed by the dependent
CG377 change before the deployment pass is complete.

## Prior validation (preserved)

# CG-339 running-application validation

Commit `eb445f6c9e48225d3895040fcc94ae6319f45dbc` was exercised with:

```sh
PYTHONPATH=src .venv/bin/python -m garden.interaction_replay --out /tmp/cg339-final-replay --head eb445f6c9e48225d3895040fcc94ae6319f45dbc --nonce durable-cg339-20260907
```

This starts the real web application from the proposed source on an ephemeral port, against a
disposable garden and pretend GitHub. The scripted operator performs HTTP GETs and form POSTs;
it is controlled interaction, not a real model harness or a visual browser session. The command
completed all nine flows and recorded 100 served HTTP requests/responses. `result.json` is the
tool-produced manifest: it contains the issued nonce, tested head, start/finish timestamps,
chronological state events, per-flow requests and responses, and durable artifact paths.

- Affected objective: the operator planned and approved work, dispatched it, answered a meaningful
  database question, sent a revision back, reconciled a justified nothing-to-change result, merged
  the work, and closed the phase. The actions and outcomes are in `result.json`; rendered responses
  are under `pages/`.
- Empty outcome: the final action fetched the Inbox and asserted its rendered `Inbox zero` outcome;
  the response is `pages/0023-index.html`.
- Failure/recovery: the `send back with a note` flow POSTed a changes request, observed the task in
  changes requested with a revise action, dispatched recovery, and observed it return to triage.
  The task responses are `pages/0011-tasks-dm-003.html` through
  `pages/0013-tasks-dm-003.html`.
- No-change as an outcome: the `reconcile a nothing-to-change report` flow sent DM-002 back, ran the
  revision, and observed the garden reconcile it without presenting an internal-status decision.
  See `pages/0015-tasks-dm-002.html` and `pages/0016-tasks-dm-002.html`.

Automated checks are separate: the focused pytest cases validate applicability, stale/missing/failed
evidence rejection, artifact presence, and the scalability extension. This run did not use a visual
browser and therefore makes no layout claim. `tick-log.json` preserves the scheduler activity from
the disposable run.
