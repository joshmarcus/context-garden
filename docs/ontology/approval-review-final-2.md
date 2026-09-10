## Review

The five requested corrections are present and source-supported:

- Phase persona reviews are phase-scoped runs without a task document or PR.
- `waiting` and `env_error` are modeled, with lifecycle completion distinguished from archival eligibility.
- Task/event and harness/model links are optional; usage remains an open mapping.
- Resolved authorities are explicitly not unique per run.
- The workload-identity authority and trust-boundary model is retained.

One remaining contradiction requires correction:

- `Brief` is defined as containing a task, but phase persona prompts are built by `phase_brief` without a task document. The ontology should qualify this definition or model phase persona prompts separately.

ONTOLOGY_REVIEW: {"verdict":"request_changes","reviewed_head":"bcc8419c2dde10ff022621a8a73d37bebdb8d1bc","summary":"Requested ontology corrections are present, but the universal Brief definition contradicts taskless phase persona prompts.","findings":["ONTOLOGY.md states that every Brief contains a task, while src/garden/scheduler/persona.py and src/garden/personas.py construct phase persona prompts without a task document."]}