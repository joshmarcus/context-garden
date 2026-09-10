# Maintainer review of the canonical ontology

Review `ONTOLOGY.md` against commit `bcc8419c2dde10ff022621a8a73d37bebdb8d1bc` as a
staff engineer responsible for the model, persistence, scheduler, worker protocol, and host
lifecycle code. Work read-only and do not edit or commit files.

Check factual accuracy, completeness of identities/attributes/null semantics/cardinalities/
lifecycles/authority/retention, Mermaid syntax and readability, source references, internal
consistency, and whether discrepancies are reported rather than invented away. Pay special
attention to `src/garden/model.py`, `store.py`, `graph.py`, `runs.py`, `events.py`, `checks.py`,
`validation.py`, `review.py`, `personas.py`, `scheduler/state.py`, `scheduler/review.py`,
`scheduler/human.py`, `remote_worker.py`, `managed_worker.py`, `hosts/`,
`docs/architecture.md`, `docs/worker-protocol.md`, and `docs/host-lifecycle.md`.

Return a concise Markdown verdict with blocking errors first. End with exactly one line:

`ONTOLOGY_REVIEW: {"verdict":"approve"|"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"<summary>","findings":[{"severity":"blocking"|"non_blocking","location":"<section>","problem":"<specific issue>","correction":"<specific correction>"}]}`
