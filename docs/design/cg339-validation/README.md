# CG-339 running-application validation

Code commit `a7cf00bd1050330e701c70919c30b28024cb77b5` was exercised with:

```sh
PYTHONPATH=src .venv/bin/python -c 'from pathlib import Path; from garden.qa import run_qa; r=run_qa(Path("docs/design/cg339-validation"), scripted=True, keep=False, log=print); print(r.summary()); raise SystemExit(0 if r.ok else 1)'
```

This starts the real web application from the proposed source on an ephemeral port, against a
disposable garden and pretend GitHub. The scripted operator performs HTTP GETs and form POSTs;
it is controlled interaction, not a real model harness or a visual browser session. The command
completed all nine flows.

- Affected objective: the operator planned and approved work, dispatched it, answered a meaningful
  database question, sent a revision back, reconciled a justified nothing-to-change result, merged
  the work, and closed the phase. The actions and outcomes are in `result.json`; rendered responses
  are under `pages/`.
- Empty outcome: `findings.json` is an empty list after every flow completed, while the final phase
  and herbarium responses show the completed/closed state (`pages/0021-phases-demo-p1.html` and
  `pages/0022-herbarium.html`). This does not claim a visual empty-Inbox inspection.
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
