# Worker fleet rollouts

`garden.worker_rollout.WorkerRollout` is the supported orchestration API for installing an
already-published Garden version on an existing managed-worker fleet. It does not publish a
release, provision capacity, alter enrollment, or authorize task dispatch.

## Release and backend prerequisites

The caller supplies a numeric immutable package version, its full 40-character source commit,
and a non-empty, independently verified path-to-SHA256 source manifest. Mutable labels,
abbreviated commits, unverified manifests, and incomplete candidates are rejected before the
fleet is inventoried. The worker backend must install into a new versioned runtime while
retaining the prior runtime and must implement host-local observation, staging, activation,
health, rollback, and fencing. Its attestations are treated as evidence, not inferred from a
process merely existing.

`CommandWorkerRolloutBackend` connects the API to existing fleet tooling. It executes a fixed,
operator-approved argv with a bounded timeout, sends each action as JSON on stdin, and reads one
JSON attestation from stdout. It never invokes a shell or places candidate/worker facts in argv;
credentials remain in the command's preconfigured environment or host-local files.

Inventory is a sequence of existing `WorkerTarget` records. Those records preserve the stable
worker and host identities, service ownership/configuration, resource caps, absolute deadline,
and enrollment and secret boundaries. The plan additionally records the aggregate budget and
controller runtime. Rollout never creates, replaces, or resizes a host and never changes those
values.

## Operation lifecycle

Create a `RolloutStore` in controller-owned durable storage and call `plan` before `start`.
`plan` is idempotent for the same candidate identity and rejects a different candidate while a
journal exists. `status` returns the complete operator-visible journal. `resume` continues the
same operation and skips workers with completed receipts. `abort` prevents any subsequent
mutation; it does not interrupt a worker or undo already completed workers.

Workers advance in inventory order. The journal records every transition and attestation before
the next mutation: planned, waiting-for-idle, draining, staged, verified, activated,
health-checking, complete, deferred, failed, and rolled-back. The backend's claim generation is
checked immediately before activation. A busy worker, pending result collection, or changed
generation is deferred without switching its service. Active worktrees, branches, source,
transcripts, results, and collection remain owned by the worker protocol and are never modified
by rollout.

Staging proves the package version, direct-url commit, exact source manifest, executable, and
required tool environment. Activation separately proves those identities plus the admitted unit
source, config path, owner/group/mode, executable/runtime binding, advertised source head, thawed
unit, new PID, and readiness. Success then requires multiple authenticated claim, heartbeat, and
finish compatibility observations over a bounded stability window, with no restart or immediate
resource failure.

The first failure stops the fleet. If a fresh observation still proves the affected worker idle
and the retained prior runtime is known, rollback is attempted and verified. A worker that became
busy is never restarted or rolled back: it is fenced with an explicit collection-first recovery
instruction. Failed rollback likewise leaves the worker fenced and names the retained runtime to
repair and verify before thawing. Later workers remain untouched and completed receipts remain
valid for a later inspection or resume.

Production backends should keep credentials behind their existing host-local references. Neither
targets, observations, nor receipts should contain secret values. A disposable backend can drive
the same non-destructive claim/heartbeat/finish journey to validate protocol compatibility without
claiming production work.
