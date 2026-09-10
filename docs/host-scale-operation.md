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

## Verification scope

Focused tests exercise interrupted requests and API responses, immutable admission,
budgets, identity mismatches, expired enrollment, partial success, served worker protocol
journeys, and cleanup. Mocked OS-manager tests verify contracts; they do not prove a macOS
deployment or an AWS clean-image rollout. Acceptance uses proportionate deterministic and
disposable validation. Live cloud canaries are optional, never an acceptance, merge, or
phase-closure gate. Any optional rollout must identify its exact candidate; earlier fleet
receipts cannot prove that a later implementation was deployed.
