# Multiplayer HTTP authorization

Multiplayer mode authenticates every non-public HTTP request as a garden member installation.
The route policy is deliberately split between admission and projection: middleware resolves
resource ownership and rejects direct access outside the principal's projects, while collection
handlers construct their response only from the principal's visible project set.

## Local installation startup

An administrator bootstraps the coordinator's private registry, then adds a member and issues
that member's installation credential.  The commands print credentials once; put each value in
that person's shell secret store, never in a garden file.

```sh
garden members enroll-administrator garden-1 admin coordinator-host
export GARDEN_ADMIN_CREDENTIAL='printed-once-value'
garden members add alex --role member --credential-env GARDEN_ADMIN_CREDENTIAL
garden members issue-installation alex alex-laptop --credential-env GARDEN_ADMIN_CREDENTIAL
```

On Alex's local checkout, save only the connection metadata.  `connect` verifies the credential
before writing the ignored `garden.local.yaml`; it does not copy the credential into that file.

```sh
export GARDEN_ALEX_CREDENTIAL='printed-once-value'
garden members connect garden-1 https://coordinator.example alex alex-laptop \
  --credential-env GARDEN_ALEX_CREDENTIAL
garden members status
```

An administrator assigns execution explicitly on the coordinator host (the generation prevents
silently replacing a newer assignment):

```sh
garden members assign alex context-garden phase-10 \
  --credential-env GARDEN_ADMIN_CREDENTIAL --generation 0
```

Before that assignment, `garden watch` is supported: it reports **No work assignment** and
performs no scheduler work.  A viewer session is read-only and `garden watch` refuses to start
it.  `garden serve` follows the same local-principal rule, so a viewer never starts its embedded
scheduler.  `garden members status` distinguishes a missing credential, disconnected
coordinator, cached/disconnected authority, and a valid unassigned installation.

| Read surface | Principal policy | Response boundary |
| --- | --- | --- |
| health, favicon, plate assets | public | contains no garden records |
| config | administrator | garden-wide operational configuration |
| board, inbox, events, costs, runs, trellis, herbarium, Now and their partials/stream | authenticated project reader | tasks, events, runs, costs, products and aggregates are projected to visible projects |
| task, run, operation, investigation, transcript and capture detail | authenticated reader of the owning task's project | the task ID is resolved before admission; aliases and artifacts inherit that ownership |
| phase pages and phase documents | authenticated reader of the named project | the project path is resolved before admission |
| API task and decision collections | authenticated project reader | rows are projected to visible tasks/projects |
| worker/controller status, maintenance, diagnostics and API worker/event collections | administrator | operational garden-wide data is not project-attributable |
| framework docs, design browser, trials | administrator | currently garden-wide; these surfaces do not yet provide a complete project projection |

## Recoverable migration from standalone mode

Cutover is explicit and fail-closed. First stop every `garden watch`/`garden serve` process,
let active attempts finish (or cancel and reconcile them), and commit or stash local edits in
the garden and its product repositories. On the coordinator host, bootstrap membership and run
the private service. A non-loopback listener requires a certificate and key.

```sh
garden members enroll-administrator garden-1 admin coordinator-host
export GARDEN_ADMIN_CREDENTIAL='printed-once-value'
garden migration serve-coordinator --host 10.0.0.8 --port 8770 \
  --tls-certfile /run/secrets/garden.crt --tls-keyfile /run/secrets/garden.key
garden members connect garden-1 https://10.0.0.8:8770 admin coordinator-host \
  --credential-env GARDEN_ADMIN_CREDENTIAL
```

Enroll every person with `garden members add` and issue one operating installation with
`garden members issue-installation`. Write choices as JSON. Legacy labels may map to a member
ID or to an empty string for explicit unassignment. Phase owners are separate from task
owners/defaults; an empty phase value leaves review, kickoff, retrospective, and closure
unavailable. Installation bindings are one-to-one, preventing a shared operating identity.

```json
{
  "owner_map": {"josh": "josh", "old-bot": ""},
  "phase_owners": {"context-garden/phase-10": "josh"},
  "installations": {"josh-laptop": "josh", "alex-laptop": "alex"}
}
```

`preview` records no authority changes. It reports unknown owners, explicit/unassigned
mappings, phase defaults, active attempts, pending effects/projections, dirty source, and setup
work. Commit only the returned content-addressed ID:

```sh
garden migration preview --choices migration.json
garden migration commit --preview-id 0123456789abcdef \
  --credential-env GARDEN_ADMIN_CREDENTIAL
garden migration status
```

Commit holds the controller lock, rechecks the preview, saves a consistent standalone archive,
then seeds versioned task and phase authority. Its journal advances after every scope, so the
same commit command resumes an interruption. The final `.garden/authority-mode.json` fence is
independent of `garden.yaml`: turning `multiplayer.enabled` off cannot revive an old scheduler.
Keep the printed recovery archive until the multiplayer garden has been verified.

Assign each user's execution cursor and, independently, phase-operation ownership:

```sh
garden members assign alex context-garden phase-10 --generation 0 \
  --credential-env GARDEN_ADMIN_CREDENTIAL
garden members assign-phase context-garden phase-10 alex --generation 0 \
  --credential-env GARDEN_ADMIN_CREDENTIAL
```

Each person uses `garden members connect`, then `garden members status` and their local
`garden serve` or `garden watch`. Publish anonymous data only through the isolated projection:

```sh
garden publish --output /srv/garden-public
garden serve --public-projection /srv/garden-public --no-watch --host 127.0.0.1
```

For reversal, stop all local schedulers and reconcile claims, unknown effects, and pending
projections. Export one standalone authority outside the live garden, inspect it, then restore
that archive into a separate checkout before starting a legacy watcher:

```sh
garden migration export-standalone --destination /safe/recovery/garden-standalone.tar.gz
```

The coordinator is a single availability authority: authenticated reads may show a last-known
snapshot during an outage, but mutations stop. It assumes enrolled operators and the TLS/network
boundary are trusted; it does not provide hostile multi-tenant isolation. Recovery archives
retain historical task documents, events, runs, raw results, failed checks, and reviews as
legacy attribution. They exclude credentials and do not relabel historical authors as
authenticated members.

An `all` visibility principal can read every project. An `assigned` principal can read only the
explicit project keys in the private member registry; an empty assignment has no project read
access. Mutations remain separately authorized as administrator operations or work owned by the
authenticated member. Legacy single-user mode does not install this policy.
