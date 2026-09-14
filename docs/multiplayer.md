# Multiplayer Git coordination

The normative contract is [True multiplayer gardens](../specs/multiplayer.md). Multiplayer
does not require a Garden coordinator daemon, HTTP effect gateway, or shared database. Every
installation uses the Garden context repository's remote to conditionally advance one
dedicated state branch.

## Configuration

Multiplayer is an explicit, garden-wide startup choice. Omission keeps a garden that has
never enrolled in its existing standalone mode.

```yaml
multiplayer:
  enabled: true
  garden_id: garden-1
  member_id: alex
  installation_id: alex-laptop
  git:
    remote: origin
    state_ref: refs/heads/garden-state
```

`remote` names an existing remote in the Garden context checkout, not a product checkout.
The configured ref must be a full branch ref. Enabling this setting does not create the
branch, enroll a member, assign work, or grant authority. A missing, deleted, incompatible,
or rewritten state ref makes shared mutation unavailable.

The Git host credential is installation-private. Members and public installation bindings
are committed, but secrets, heartbeats, raw transcripts, and provider credentials are not.
Protect the state branch from deletion and non-fast-forward updates and exclude routine state
pushes from product CI.

## Administrator initialization and admission

There is currently **no supported operator CLI workflow** to initialize a new Git state branch
with its first administrator, admit members and installations, or write their Git-backed
assignments. Do not substitute the legacy coordinator workflow or edit the state branch by hand;
keep the garden standalone until the Git bootstrap and administration workflow lands.

In particular, `garden members enroll-administrator`, `add`,
`issue-installation`, `assign`, `assign-work`, and phase-owner assignment commands update
the legacy local member registry. They do not administer the Git coordination branch and must not
be used as a Git multiplayer setup recipe. The intended model remains one effective owner per
scope: accepted phase ownership is inherited by tasks unless a task has an explicit override, and
an administrator may explicitly unassign a task or phase. Assignment is not the project selector
and does not grant work to another installation.

Likewise, `garden migration preview`, `commit`, `export-standalone`, and
`serve-coordinator` are coordinator-era commands, not supported Git multiplayer migration or
reversal paths.

## Admitted-user startup

For an already admitted installation, the supported local startup command is:

```sh
garden members connect-username garden-1 --installation-id alex-laptop
garden members status
```

`connect-username` binds the checkout to the effective operating-system account and validates
that its named installation is already admitted on the configured state ref. It writes only local
connection metadata to `garden.local.yaml` and does not enroll a user, create a branch, assign
work, or start a scheduler. It deliberately avoids a routine browser login or enrollment flow and
fails closed when the branch, admission, or Git access is not valid.

`garden members connect garden-1 alex alex-laptop` is available when an already-admitted
installation uses its private credential rather than temporary username binding. It has the same
preconditions and does not bypass admission. An enabled assignment allows normal scheduler entry
points to attempt work only after the Git authority transaction succeeds. An absent or paused
assignment is idle: starting the web UI or `watch` creates no work. Do not enable operation by
altering `garden.local.yaml` alone.

## Runtime protocol

`garden.git_coordination` fetches and validates the complete linear state history, constructs
a single-parent child of the exact observed tip, and pushes it using an explicit
expected-predecessor lease. A contention loser fetches again and revalidates semantic
preconditions; it never merges competing state commits. Stable operation IDs make retries
idempotent and resolve a lost push acknowledgement from accepted history.

Claims, execution/effect permits, and shared reservations that jointly authorize a launch
belong in one transaction. No protected process or provider effect starts before that commit
is accepted. Effect intent is recorded before a local provider call; pending or unknown
outcomes remain handoff barriers until reconciled. Client clocks and Git commit timestamps
do not transfer ownership.

Local `.garden/coordination-observed` records the last validated tip. Evidence for duplicate
operation mismatches, deletion, rewrites, and malformed ancestry is retained under
`.garden/coordination-recovery/`. Those local files aid recovery but never grant authority.

## Blocked handoffs and recovery

An ownership change is a single acknowledged handoff, not an assignment race. The effective owner
or an administrator starts it; the former installation stops its claimed work and acknowledges
that stop; pending or unknown permits, effects, and projections are reconciled; only then can the
handoff change authority and the new owner claim work. Late output remains stale evidence rather
than a result for the new owner.

There is currently **no supported manual Git handoff or recovery CLI** for an administrator. If a
handoff is blocked, stop the old local workload, restore access to the protected state ref, and
retain `.garden/coordination-recovery/` and `.garden/coordination-observed` for investigation.
Do not force-push, delete, recreate, or manually edit the state ref; those actions do not restore
authority and leave shared mutations fail-closed. Keep the scope unassigned or with its current
owner until the supported administrative recovery workflow lands.

## Unsupported migration and reversal

Standalone-to-Git migration and standalone reversal are not supported in this release. Do not use
the `garden migration` commands for a Git multiplayer cutover or recovery: they require the
rejected coordinator-service architecture. They remain in CLI help only for legacy compatibility.
A production multiplayer activation needs a separate owner decision after the Git migration and
recovery implementation is verified.

## Availability and trust boundary

If the remote is unavailable, previously fetched information may be displayed as stale, but
new claims, launches, publication, and shared reservations wait. Workers do not receive the
state-ref write credential. Git write access is a trusted-team boundary: Garden validates
records and transitions, but repository readers can inspect committed coordination metadata.
Use separate private repositories where that visibility is inappropriate.
