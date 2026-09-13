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
branch, enroll a member, assign work, or grant authority. Initialization is an explicit
administrative operation; a missing, deleted, incompatible, or rewritten state ref makes
shared mutation unavailable.

The Git host credential is installation-private. Members and public installation bindings
are committed, but secrets, heartbeats, raw transcripts, and provider credentials are not.
Protect the state branch from deletion and non-fast-forward updates and exclude routine state
pushes from product CI.

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

## Availability and trust boundary

If the remote is unavailable, previously fetched information may be displayed as stale, but
new claims, launches, publication, and shared reservations wait. Workers do not receive the
state-ref write credential. Git write access is a trusted-team boundary: Garden validates
records and transitions, but repository readers can inspect committed coordination metadata.
Use separate private repositories where that visibility is inappropriate.
