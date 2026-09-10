# Persona reviews

Personas add a focused perspective to normal PR and phase review. Run a named one with
`garden persona-review -p <name> TASK_OR_PHASE`, require it from a task with
`persona-review -p <name>`, or configure a recurring PR review in `review.personas`.
`garden personas` lists built-ins and local customizations. `garden init` writes built-in
personas to `personas/` without replacing an existing file, so a garden can tailor any role.

PR reports are comments; phase reports are durable markdown under
`<phase>/docs/reviews/`. Their structured findings can enter the normal revision flow or be
filed as draft tasks. A persona report is advice and evidence, not an unreviewed change to
the product's source of truth.

## The ontologist

Use the **ontologist** when a change introduces or reshapes domain concepts, persisted
records, state machines, identifiers, APIs or protocols, or when a model has become hard to
extend without breaking callers. It examines vocabulary, entity/value distinctions, identity
and scope, relationships and cardinality, lifecycle semantics, invariants, authoritative and
derived data, and persistence/protocol representations.

The ontologist reports concrete, source-grounded findings and an authored **Ontology
specification** section. That narrative can include a compact diagram or model examples and
is retained in the usual phase report or PR comment. Turn its corrections into ordinary
reviewed tasks or revisions; keep the specification with the work that adopts it.

The ontologist owns conceptual precision and representation evolution. It complements rather
than replaces staff-engineering review (architecture, implementation quality and operations),
product review (user value and roadmap), and security review (trust boundaries and attack
risk). Route concerns in those areas to their respective reviewers.
