## Review

`ONTOLOGY.md` correctly:

- Supports phase persona reviews without a task document or PR.
- Includes `waiting` and `env_error`, distinguishing lifecycle completion from archive eligibility.
- Treats harness/model links as optional and usage as one open mapping.
- Explicitly avoids per-run uniqueness for resolved authorities.
- Retains the workload-identity authority model.

One concrete contradiction remains: the Event definition says `task` is a required emitted field, while `EventLog.emit()` defaults `task_id` to `""` and the ontology’s relationship diagram models the task/event link as optional. This should be clarified before treating the document as canonical.

ONTOLOGY_REVIEW: {"verdict":"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"The requested ontology corrections are present, but Event.task is incorrectly described as required despite optional task linkage in the implementation.","findings":["ONTOLOGY.md:289 — Event.task is described as required, contradicting EventLog.emit()'s empty default and the optional task/event relationship."]}