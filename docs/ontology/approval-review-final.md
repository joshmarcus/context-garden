## Review

The five requested corrections are present and accurately grounded in source:

- Phase persona reviews are phase-scoped auxiliary runs without a task document or PR.
- `waiting` and `env_error` are included, with lifecycle completion separated from archive eligibility.
- Task/event and harness/model associations are optional; usage remains an open mapping.
- Resolved authorities are explicitly not unique per run.
- The workload-identity authority model and trust boundary are retained.

One remaining concrete contradiction requires correction:

- `ONTOLOGY.md` claims the listed run statuses are exhaustive, but `src/garden/scheduler/reap.py` assigns `run.status` to both `no_change` and `wont_do`. These statuses are then handled distinctly from task lifecycle state and archival eligibility. The run-status inventory and related lifecycle wording should include them.

ONTOLOGY_REVIEW: {"verdict":"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"Requested ontology corrections are present, but the run-status inventory omits implemented no_change and wont_do statuses.","findings":["Run statuses are not exhaustively listed: src/garden/scheduler/reap.py assigns run.status to no_change and wont_do. Update the inventory and lifecycle/archive explanation."]}