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

An `all` visibility principal can read every project. An `assigned` principal can read only the
explicit project keys in the private member registry; an empty assignment has no project read
access. Mutations remain separately authorized as administrator operations or work owned by the
authenticated member. Legacy single-user mode does not install this policy.
