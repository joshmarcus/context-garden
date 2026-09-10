# Ontologist source brief for the canonical Garden ontology

This is a named **ontologist** persona run. The accepted product source identity is commit
`bcc8419c2dde10ff022621a8a73d37bebdb8d1bc` on branch
`garden/cg-532-have-the-ontologist-author-a-top-level-ontology`. Work read-only: do not edit
or commit files.

Apply the built-in ontologist persona in `src/garden/personas.py`. Author a comprehensive,
first-person `ontology-specification` for a canonical top-level `ONTOLOGY.md`. Ground every
claim in the checked-out source and name source locations. Where representations conflict,
record an explicit discrepancy instead of harmonising them by invention.

Read these starting materials, then follow their direct source references as needed:

- `src/garden/model.py`, `store.py`, `graph.py`, `brief.py`, `runs.py`, `events.py`,
  `config.py`, `checks.py`, `artifacts.py`, `harness.py`, `remote_worker.py`
- `src/garden/scheduler/state.py` and the scheduler modules that define transitions,
  reviews, decisions, validation, remote claims, costs, and provisioning
- `src/garden/provisioning.py`, `managed_worker.py`, and `workload_identity.py` when present
- `docs/architecture.md`, `docs/design.md`, `docs/worker-protocol.md`,
  `specs/system-architecture.md` when present, and `specs/README.md`

The specification must define product, phase, task, dependency/stack, run/attempt,
review/finding/decision, check/validation, artifact, worker/host/lease,
harness/model/profile, event/cost/usage, and provisioning operation where they exist.
For each material model, cover identity/scope, attributes and types,
required/optional/default/null semantics, relationships/cardinalities, lifecycle,
invariants, authority, persistence/protocol forms, and deletion/retention. Include Mermaid
relationship and lifecycle diagrams, representative serialisations, compatibility and
extension rules, and a discrepancies/findings section. Keep it a system specification,
not a rollout plan or progress inventory.

Return the ontology narrative in Markdown followed by the normal one-line marker:

`GARDEN_PERSONA: {"persona":"ontologist","score":<0-10>,"overall":"<summary>","findings":[...],"sections":{"ontology-specification":"<the full Markdown specification>"}}`

Do not include credentials, tokens, private paths, or operational secrets.
