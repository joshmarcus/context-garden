# True multiplayer gardens

## Purpose and boundary

Multiplayer mode lets several people operate one garden from independent local checkouts. The
Garden context repository's protected Git state branch is authoritative for membership,
installation bindings, execution assignment, task and phase ownership, claims, permits, effects,
reservations, and handoffs. There is no Garden coordinator daemon, HTTP authority service, or
shared coordinator database in the accepted architecture. Local Markdown is still the
human-readable source projection, but it is not an authority source while multiplayer is active.

This is a cooperative trusted-team boundary, not hostile multi-tenant isolation. Git readers can
inspect committed coordination metadata; use a separate private repository where that visibility
is inappropriate. Source acceptance does not activate a deployment, publish a garden, provision
infrastructure, or release software.

## Identity, visibility, and execution

Membership and installation bindings are committed coordination records. An installation belongs
to one member and may not move between members. Administrators manage garden-wide authority,
members operate assigned work, and viewers cannot schedule or mutate. Admitted-user startup binds
the effective operating-system username to an already admitted installation; routine browser login
and enrollment are not part of the operating model. Project visibility, the UI's selected project,
and execution assignment are separate facts. The selector filters ordinary views without changing
the member's assignment. A personal Inbox contains only that member's actionable cards; team cards
are read-only. Direct actions recheck current Git authority instead of trusting a form, URL, or
previously rendered page.

Each member has at most one versioned execution cursor: project, phase, enabled state, and whether
automatic advance is permitted. An absent or disabled cursor is safely idle. Starting `watch` or
the web UI while idle creates no task, run, pull-request, CI, phase, or resource operation.
Dependency, hold, freeze, review, CI, merge, budget, and resource gates still apply.

## Fenced work and recovery

Task and phase ownership are separate, versioned authority records. A task follows accepted phase
ownership until an explicit task override changes it; explicit unassignment is valid. Before a
lifecycle mutation, a scheduler records its claim, required permits, and reservations in one
conditional Git transaction. A second installation cannot own the same active scope. Claims carry
a monotonically increasing fence; an old owner, generation, revision, or fence is rejected.

Reassignment is an acknowledged handoff. The new assignee waits until the old worker stops and
every pending or unknown provider effect and source projection is reconciled. Late output is
retained as stale evidence, never adopted as the current result. Failed projections remain
inspectable and retryable; unknown external outcomes block a repeat effect. Stable operation IDs
make retries idempotent and resolve a lost push acknowledgement from accepted history.

During a disconnect, a UI may show an installation-bound last-known snapshot labelled stale, but
scheduling and mutations fail closed. After reconnect, the scheduler refreshes Git authority,
acknowledges cancellation only after local work stops, and continues only work admitted by the
current assignment and fence.

## Explicit phase ownership

Every phase-wide workflow has one explicit owner independent of task defaults and assignees. Only
an active installation of that owner may initiate kickoff, phase review, retrospective, closure,
or reopening. Git coordination admits one phase claim; idempotent retries resolve to the original
operation, while concurrent requests and another installation cannot create a second parent.

Unassigned, disabled, or replaced owners cannot start phase work. Reassignment fences the old
phase claim and child effects. Child reviews retain separate records and scopes, while closure
still waits for ordinary task, review, CI, hold, and evidence gates. Finishing the last task does
not transfer phase authority to that task's owner.

## Public projection

Anonymous viewing is served by an isolated application from an explicitly generated projection
directory. It has no private `Store`, Git authority, scheduler, worker ingress, configuration,
run artifacts, or mutation routes. Publication is default-empty and includes only allowlisted fields;
free-form task content requires opt-in and still excludes the private log. Replacing the projection
with an empty project set revokes subsequent reads and existing streams.

The projection is a publication snapshot, not a live authorization layer. Revocation takes effect
when an operator regenerates and atomically replaces it; it cannot retract data already copied,
cached, or observed.

## Migration and operation

Standalone gardens remain supported until explicit Git enrollment and cutover. Git migration and
standalone reversal are not supported for production use until their Git-authority implementation
is verified. The coordinator-era `garden migration` commands must not be used for a Git
multiplayer cutover or recovery. A missing, deleted, rewritten, malformed, or incompatible state
ref fails shared mutation closed; local evidence can help investigation but cannot restore
authority.

Commands, transport requirements, and recovery steps are in
[the multiplayer guide](../docs/multiplayer.md).
