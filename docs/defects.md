# Closed-task defects

A defect is a later observation attached to a terminal task. It does not reopen the task,
change its result or review, start a run, or create corrective work. Minor means limited
impact or a practical workaround; major means substantial impact or failure of core behavior.
The reporter makes that classification. Recording a defect does not infer a cause.

The closed task page offers **Record defect…** and shows every defect, its disposition, and
its correction history. Records live in the private, process-safe `.garden/defects.json`
ledger. A linked follow-up is only a reference to separately authorized work.

CLI examples:

```sh
garden defect-record CG-123 --severity major --description "Export omitted rows" --reporter alice \
  --expected "All selected rows" --observed "Only the first page" --impact "Incomplete report" \
  --evidence-link https://example.invalid/evidence --idempotency-key incident-42
garden defect-list --product context-garden --phase phase-07 --severity major --disposition unreviewed
garden defect-update DEF-... --expected-revision 1 --actor alice \
  --known-facts "Pagination stops after one request" --hypotheses "" --unknowns "Affected versions" \
  --could-have-caught "API integration test" --prevention "Paginated export test" \
  --proposed-follow-up CG-999 --disposition reviewed
```

The JSON API mirrors this workflow:

- `POST /api/tasks/{task_id}/defects` accepts `severity`, `description`, an optional
  `idempotency_key`, and the optional record fields shown by the CLI. Authentication supplies
  the reporter; a repeated key and identical payload returns the original defect.
- `PATCH /api/tasks/{task_id}/defects/{defect_id}` requires `expected_revision` and accepts
  corrections, analysis fields, severity, or `disposition`. Stale revisions return `409`.
- `GET /api/defects` filters by `severity`, `product`, `phase`, `task_id`, `disposition`,
  `discovered_from`, and `discovered_to`. Its summary counts defect IDs, never edits.

Known facts, hypotheses, and unknowns are separate fields. Analysis may identify what could
have caught the issue, recurrence prevention, and proposed follow-up without asserting blame
or causality. Project visibility is applied to list results and the normal owned-task mutation
policy applies to create and update operations.
