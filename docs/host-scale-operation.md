# Resumable production worker scaling

`garden hosts scale` admits one bounded pool request, reports its remaining setup, and
continues that same durable operation after interruption. Install the controller with the
`aws` extra (`pip install 'context-garden[aws]'`). The pool JSON uses the versioned schema
in [host-lifecycle.md](host-lifecycle.md).

```sh
garden hosts scale pool.json --deadline 2030-01-01T20:00:00Z \
  --aggregate-limit 80 --enrollment-config /private/garden/production.json
garden hosts scale pool.json --continue
garden hosts scale pool.json
garden hosts scale pool.json --cleanup
```

The example date is a placeholder, not authorization to run a fleet until that date.
`--deadline` records admission without launching a host; `--continue` performs the missing
steps. These actions are mutually exclusive. Enrollment configuration and directory
references are saved, so subsequent commands can omit them. `--state` selects another
operation file; its containing directory defines the aggregate ledger. Use one ledger
for the entire allocation.

The immutable admission includes desired/maximum hosts, provider and resource settings,
exact `profile.source_head`, bootstrap artifact SHA256, deadline, and cost limits. A
repeated request reads that admission without resetting expiry or launching again.
Continuation rejects changes. A later admission gets a distinct per-host provider token;
another active operation for the same pool must be finished first.

Budget admission covers the greater of declared runtime and the full time until expiry.
Sibling reservations are serialized under one process/thread lock. The most restrictive
existing aggregate limit remains in force. Termination does not release historical
spending: conservative charges remain because provider bills arrive later. Prices must
include applicable costs; the adapter does not discover prices or make a projection a
provider billing cap.

The admitted account, region, provisioning profile, repository, and instance tags are
also fixed. Editing the credential-reference configuration cannot redirect continuation
into a different account or region.

## Saved, scoped enrollment

The production configuration contains credential **file/profile references**, not inline
administrator credentials. Its AWS boundary is the reviewed
`context-garden/phase05/renew-*` namespace. A private configuration looks like:

```json
{
  "aws_profile": "garden-provisioner",
  "aws_region": "us-east-1",
  "aws_account_id": "YOUR_ACCOUNT_ID",
  "tailscale_oauth_file": "/private/garden/tailscale-oauth-client.json",
  "github_token_file": "/private/garden/repository-management-token",
  "github_repo": "owner/repository",
  "model_auth_files": {"workers-0": "/private/garden/model-worker-0.json"},
  "model_identities": {"workers-0": "dedicated-worker-0"},
  "secret_tags": {
    "ManagedBy": "context-garden", "Pool": "phase05", "Purpose": "worker-bootstrap"
  },
  "instance_tags": {"ManagedBy": "context-garden", "Pool": "phase05"}
}
```

Use absolute paths appropriate to the controller. The operation ownership tag is derived
from the admitted generation; a conflicting supplied tag is refused. The AWS profile must
resolve to the scoped `ContextGardenProvisioner` assumed role in the selected account.
This command grants no IAM privileges. The Tailscale OAuth client needs only `auth_keys`
for `tag:garden-worker`. Repository management stays on the controller. Workers receive
separate repository deploy keys, model credentials, single-use tailnet enrollment, and
controller tokens. Private repositories also need `repository_private: true` and per-host
`ci_read_token_files` with scoped CI read tokens. Management credentials never enter a
worker bootstrap payload.

Each host needs a distinct mode-0600 model-auth input and identity label. An explicit
`model_expires_at` must cover the admitted deadline; a renewable host-owned account session
may omit it. The resolver does not obtain an interactive account session or switch to
separately billed API authentication automatically.

The enrollment directory defaults to `.garden/hosts/enrollment`, with mode-0600 journals
inside a mode-0700 directory. Durable intents precede mutations. Retries reuse repository
keys and AWS version tokens; a lost one-time tailnet secret is revoked and its absence
confirmed before replacement. Public metadata contains only labels and references.
If prelaunch enrollment expires, the same operation can refresh its owned bootstrap
secret with a journaled `PutSecretValue` version while retaining repository/controller
identities. This requires a narrowly scoped `secretsmanager:PutSecretValue` grant in
addition to create/read/delete. Missing permission is reported; the command never grants
it or requests a new administrator session automatically. Already active hosts are not
reenrolled during convergence.
The controller reads `controller-hosts.json` afresh using token hashes, so enrollment and
revocation need no app restart. For a custom directory, configure
`workers.enrollment_registry` with its absolute registry path. That is an operator setup
action; workers never write scheduler configuration.

Without a production configuration, the directory resolver supports an external
provisioner. It reports missing identities and pending revocations; file creation/deletion
alone never counts as a remote credential mutation.

## Bootstrap, readiness, and deadlines

Use a pinned AMI and credential-free HTTPS bootstrap URL with an exact SHA256. The profile
requires a full source commit separate from the profile version. Before `run_instances`,
Garden durably arms and verifies an independent termination schedule. The default uses
persistent user-systemd timers on Linux/WSL or a LaunchAgent checker on macOS. An explicit
`deadline_command` argument list can select an existing external scheduler. See
[host-deadline-scheduler.md](host-deadline-scheduler.md) for setup and platform requirements.

EC2 user-data installs an additional persistent absolute host timer before bootstrap
network work. Instance shutdown is configured to terminate. Bootstrap verifies the timer
and matches its secret to the admitted host, operation, source, artifact digest, resources,
and deadline. CPU/memory service caps come from the declaration.

`bootstrapping` becomes `ready` only after authenticated durable evidence proves exact
installed source/bootstrap, worker-owned browser setup, repository/CI reads, and a
successful `work` or `revise` task whose result and zero exit status reached the controller.
A claim, EC2 running flag, old boolean attestations, or check-only result does not pass.
Missing enrollment in one slot does not stop another prepared slot or retire a healthy
sibling.

Cleanup waits for termination before revoking enrollment, inventories retained AWS
volumes/interfaces/addresses, and keeps failed revocations visible. Secret deletion uses
the seven-day recovery window. Original model-auth inputs are preserved; successful
cleanup removes secret values from the operation's journal. Auth-key deletion does not
claim deletion of retained Tailscale device records.

## Maintaining a declared healthy count

An operator who does not want to run `--continue` by hand can declare the count Garden
should keep, inside an admission that already exists. The block is versioned and strict;
an unknown key, a missing contract version or an out-of-range count is refused rather
than ignored, so a configuration mistake never becomes a provisioning request.

```yaml
workers:
  pool:
    contract_version: garden.fleet/v1
    declaration: pool.json        # the admitted pool declaration, relative to the garden
    desired: 2                    # healthy hosts to maintain, within the admission
    interval_seconds: 300         # ordinary reconciliation cadence
    backoff_seconds: 60           # first delay after a failed pass
    backoff_ceiling_seconds: 1800 # bound on that delay
    failure_threshold: 5          # consecutive failures that stop replacement
    # state: operations/scale.json  # optional: an admission kept outside the convention
```

The operation the controller resumes is the one `garden hosts scale` writes:
`.garden/hosts/<pool name>-scale.json`, named for the pool in the declaration rather than
for the file the declaration is kept in. `state` overrides that path and must match the
`--state` the admission used.

A garden without this block keeps its static `workers.hosts` behavior and reconciles
nothing. With it, the controller resumes that one durable operation on startup and once
per tick at the configured cadence: at most one step per pass under the tick lock, so
there is no concurrent or duplicate provision request. Newly healthy hosts become
dispatchable through the dynamic worker registry, and draining, retired, expired or
unhealthy hosts stop receiving new work without restarting Garden. A failed host is
drained where needed and replaced in its stable slot; an uncertain provider response is
reconciled through discovery rather than by creating a second host for the slot.

Repeated failure backs off exponentially to the ceiling and then stops replacement
entirely, leaving healthy siblings serving. `garden hosts fleet` prints the reading;
`garden hosts fleet --resume` clears that stop once the image, credential, health probe
or provider access is fixed, and `--converge` takes one step immediately.

Configuration is not admission. A `desired` above the admitted count is clamped to the
admission, and an edited declaration is not adopted: both keep the admitted pool running
and report one concrete action — admit the change with `garden hosts scale`. The same
holds for the spend limit, the exact profile version, the identity envelope, and the
absolute deadline: when the admission expires the count stays unmet until an operator
admits a new operation. Reducing `desired` drains the excess, waits for active work to
reach a safe boundary, retires the capacity and reports any retained resource or pending
credential revocation. Ordinary reconciliation never interrupts active work.

`garden status`, `garden doctor`, `garden observe`, the workers page and `/api/workers`
all read one durable projection the controller wrote on its last pass — desired, healthy,
dispatchable, pending, draining, failed, next retry, exact worker version, deadline,
estimated cost and any action required. Those reads contact no host and no provider. The
workers page's own `dispatchable` total spans the whole fleet: managed hosts the scale
reading vouches for plus configured pull and static workers, each counted once.

Statically configured `ssh.hosts` entries appear in the same projection. A managed host is
reported by its own agent; a static host is not, so a tick starts a bounded read-only probe
of every static host at once in a detached process it never waits for, and caches one reading
per host — latency, last success, failure reason and staleness — on the
`ssh.probe_interval_seconds` cadence. The probe only asks whether the required tools and the
declared checkouts are present; it never fetches, formats or writes. See
[the worker protocol](worker-protocol.md) for the cache and its two settings.

## Verification scope

Focused tests exercise interrupted requests and API responses, immutable admission,
budgets, identity mismatches, expired enrollment, partial success, served worker protocol
journeys, and cleanup. Mocked OS-manager tests verify contracts; they do not prove a macOS
deployment or an AWS clean-image rollout. Acceptance uses proportionate deterministic and
disposable validation. Live cloud canaries are optional, never an acceptance, merge, or
phase-closure gate. Any optional rollout must identify its exact candidate; earlier fleet
receipts cannot prove that a later implementation was deployed.
