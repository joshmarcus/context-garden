# True multiplayer gardens

## Purpose and boundary

Multiplayer mode lets several people operate one garden from independent local checkouts. A
single coordinator is authoritative for membership, execution assignment, task and phase
ownership, leases, externally visible effects, and publication projections. Local Markdown is
still the human-readable source projection, but it is not an authority source while multiplayer
mode is active.

This is a cooperative, authenticated deployment boundary. It assumes trusted enrolled operators
and a trusted TLS/network boundary; it is not hostile multi-tenant isolation. Source acceptance
does not activate a deployment, publish a garden, provision infrastructure, or release software.

## Identity, visibility, and execution

Every private HTTP request authenticates one immutable member/installation binding. A member may
have multiple installations, but an installation cannot move between members. Administrators
manage garden-wide authority, members operate assigned work, and viewers cannot schedule or
mutate. Project visibility, the UI's selected project, and execution assignment are separate
facts. The selector filters ordinary views without changing the member's assignment. A personal
Inbox contains only that member's actionable cards; team cards are read-only. Direct actions
recheck identity, assignment, effective owner, and authoritative revision instead of trusting a
form, URL, or previously rendered page.

Each member has at most one versioned execution cursor: project, phase, enabled state, and whether
automatic advance is permitted. An absent or disabled cursor is safely idle. Starting `watch` or
the web UI while idle creates no task, run, pull-request, CI, phase, or resource operation.
Dependency, hold, freeze, review, CI, merge, budget, and resource gates still apply.

## Fenced work and recovery

Task and phase ownership are separate, versioned authority records. Before a lifecycle mutation,
a scheduler obtains an exclusive, server-timed claim for its member and installation. A second
installation of the same member cannot own the same active scope. Claims carry a monotonically
increasing fence; an old owner, generation, revision, or fence is rejected.

Reassignment fences the old claim and requests cancellation from its installation. The new
assignee waits until the old worker stops and every pending or unknown provider effect and source
projection is reconciled. Late output is retained as stale evidence, never adopted as the current
result. Failed projections remain inspectable and retryable; unknown external outcomes block a
repeat effect. Idempotency keys return the original result instead of creating another operation.

Coordinator state is transactional and durable across process restarts. During a disconnect, a UI
may show an installation-bound last-known snapshot labelled stale, but scheduling and mutations
fail closed. After reconnect, the scheduler refreshes authority, acknowledges cancellation only
after local work stops, and continues only work admitted by the current assignment and fence.

## Explicit phase ownership

Every phase-wide workflow has one explicit owner independent of task defaults and assignees. Only
an active installation of that owner may initiate kickoff, phase review, retrospective, closure,
or reopening. The coordinator admits one phase claim; idempotent retries resolve to the original
operation, while concurrent requests and another installation cannot create a second parent.

Unassigned, disabled, or replaced owners cannot start phase work. Reassignment fences the old
phase claim and child effects. Child reviews retain separate records and scopes, while closure
still waits for ordinary task, review, CI, hold, and evidence gates. Finishing the last task does
not transfer phase authority to that task's owner.

## Public projection

Anonymous viewing is served by an isolated application from an explicitly generated projection
directory. It has no private `Store`, coordinator, scheduler, worker ingress, configuration, run
artifacts, or mutation routes. Publication is default-empty and includes only allowlisted fields;
free-form task content requires opt-in and still excludes the private log. Replacing the projection
with an empty project set revokes subsequent reads and existing streams.

The projection is a publication snapshot, not a live authorization layer. Revocation takes effect
when an operator regenerates and atomically replaces it; it cannot retract data already copied,
cached, or observed.

## Migration and operation

Standalone gardens remain supported until explicit enrollment and cutover. Migration previews the
member, installation, task-owner, and phase-owner mapping without mutation; commit is resumable,
archives standalone source, seeds authority, and writes a fail-closed local fence. Turning config
off cannot bypass that fence. Rollback restores an export in a separate checkout only after claims
and unresolved effects have been reconciled.

Commands, transport requirements, and recovery steps are in
[the multiplayer guide](../docs/multiplayer.md).
