# CG-374 served interaction replay

The reviewed code was served from this worktree with `create_app(Store(...), watch=False)`
against a disposable garden on `http://127.0.0.1:8874`.

The current-head, disposable HTTP replay is retained in
[`replay/interaction-manifest.json`](replay/interaction-manifest.json). It records the
reviewed SHA, chronological requests and outcomes for the affected, failure, recovery,
and empty states; its accompanying `result.json`, `tick-log.json`, and captured served
pages are retained in the same directory.

- Affected state: `GET /inbox` showed the operator-owned `/etc/demo/live.yaml` prerequisite,
  its explanation, and the evidence action. `GET /tasks/DM-001` showed the same scoped
  prerequisite and action beside the task's checkout work.
- Empty state: the Inbox continued to show `Inbox zero` for decisions; the operator notice did
  not increment the `need you` count.
- Recovery state: `POST /tasks/DM-001/operator-evidence` with an on-origin request returned
  `303`; the following `GET /inbox` no longer contained the live-config prerequisite. The
  request recorded evidence only and did not give a worker production-write access.
- The interaction was also asserted by
  `tests/test_web.py::test_operator_owned_scope_is_recorded_from_the_inbox` and
  `tests/scheduler/test_human.py::test_mixed_checkout_and_live_config_work_waits_for_operator_evidence`.

I inspected these captures at 1280 and in a 390 CSS-pixel iframe (the iframe's declared
width, with no horizontal overflow visible), in both themes:

- `captures/inbox-1280-light.png`, `captures/inbox-1280-dark.png`
- `captures/inbox-390-light.png`, `captures/inbox-390-dark.png`
- `captures/task-1280-light.png`, `captures/task-1280-dark.png`
- `captures/task-390-light.png`, `captures/task-390-dark.png`
