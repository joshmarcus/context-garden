# Capability routing operations

Capability routing keeps task requirements, trusted worker authority, and host-enforced
capacity separate. Use this guide when introducing constrained tasks or changing a worker
profile. The wire details remain in [worker-protocol.md](worker-protocol.md).

## Migration and compatibility

Migrate one boundary at a time:

1. Define the capability vocabulary and deployment resource ceilings in `garden.yaml`.
2. Add a versioned `worker_configurations` profile with project/activity scope, resource
   ceilings, identity references, and operator-approved grants.
3. Enrol each physical installation as a `worker_instances` entry owned by its operating
   user. Do not copy enrollment or credential references between users.
4. Confirm fresh cached readiness and inspect `garden workers` before adding task, phase, or
   product `execution_requirements`.
5. Add requirements to a low-risk synthetic task, inspect `garden task-routing TASK_ID`, and
   only then migrate real work.

Unconstrained tasks retain the legacy claim path. Protocol-version-zero workers may claim only
those tasks; a constraint they cannot represent fails closed. Configuration contract versions
must match exactly. A profile change uses a new generation: existing instances and grants remain
fenced until they are re-enrolled and re-approved for that generation. Drain active work before
revoking or reassigning an installation. During a rolling scheduler upgrade, old and new clients
may compete because the host service, rather than one scheduler's memory, owns atomic admission
and lease generations.

## Operator workflow

Before enabling a constrained workload:

- Verify the task's effective requirements and provenance with `garden task-routing TASK_ID`.
- Verify the intended profile, active grants, readiness, ownership, and free capacity with
  `garden worker-configurations` and `garden workers`.
- Treat `busy` as a wait, `offline_or_stale` as a readiness problem,
  `no_compatible_profile` as a requirements/profile mismatch, and `denied_access` as an
  ownership or authorization boundary. Do not bypass any of these outcomes by pinning a host or
  manually changing claim fields.
- For a profile update, drain the instance, revoke its old enrollment/bindings, increment the
  profile generation, approve new grants, re-enroll it for the same operating user, and confirm
  fresh readiness before dispatch resumes.
- If a host restarts or loses its lease, confirm the old process and GPU assignment are stopped
  before capacity is reused. A late heartbeat or finish must be rejected by its replaced lease
  token. Requirement/profile drift requires a fresh author dispatch, not continuation of the old
  envelope.

The scheduler's run snapshot records an execution envelope containing the
effective-requirements digest, activity, project, owner, and selected worker, alongside the
selected profile version. Host admission additionally binds the profile revision, run, operating
user, installation, enforced resource limits, GPU devices, and monotonic lease generation. Accept
evidence only when those values match the actual run and profile. Redacted routing views are
diagnostic; they are not authorization evidence.

## Synthetic evidence versus live authority

The ordinary test suite uses fake capabilities, datasets, workers, GPUs, and host-service
responses. It proves matching, ownership fencing, reservation atomicity, lease/restart behavior,
and evidence binding in the application contract. It does **not** prove that a real GPU was
isolated, that a real restricted dataset was reachable, or that a production identity provider
issued and revoked credentials correctly.

Live checks are separate environment acceptance work. Record the exact deployment, worker/profile
generation, dataset boundary, GPU model/device assignment, enforcement mechanism, run identity,
and exported evidence. Never perform them with ambient credentials. Obtain explicit owner
authority for the named data access and identity scope, and explicit spending/provisioning
authority before starting paid GPU or cloud capacity. Without both approvals, report these checks
as remaining environment validation rather than weakening the synthetic result or attempting the
access.
