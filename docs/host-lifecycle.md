# Host lifecycle API

`garden.hosts` is a scheduler-independent API for bounded remote hosts. Its v1 contract
(`garden.hosts/v1`) separates a portable pool and environment profile from provider options.
The initial EC2 adapter accepts a boto3-compatible client supplied by the caller, so an
organization retains control of SSO, role assumption, account boundaries and SDK versions.
Neither importing configuration nor `plan()` launches or retires anything. A caller reviews
the returned cost assumptions and changes `enabled` to `true` before `reconcile()` may mutate
infrastructure. Defaults are zero desired/minimum hosts and a maximum of one; raising that
bound is an explicit configuration change after capacity has been measured.
The plan multiplies the adapter's current hourly estimate by `estimated_runtime_hours`, and
reconciliation refuses to launch when that estimate exceeds `spend_limit_usd`.

The lifecycle exposes `plan`, `provision`, `reconcile`, `inspect`, `stop`, `start`, and
`destroy`. Providers advertise stop/start, persistent-disk and Spot capabilities. Stable
owner, pool, host and operation labels make a repeated request or controller restart discover
the first request before launching another host. Events and facts use explicit provisioning,
bootstrapping, ready, busy, draining, interrupted, failed, stopped and terminated states.

## EC2 worker profile

Pin the AMI ID, profile version and bootstrap version; do not use a moving image or package
label. Put EC2-only settings under `provider_options`:

```python
profile = EnvironmentProfile(
    name="garden-worker", version="1.0.0", image="ami-0123456789abcdef0",
    bootstrap_version="0.1.0+abc123", cpu=4, memory_mib=16384, disk_gib=40,
    endpoint="https://garden.example.com",
    enrollment_secret_ref="arn:aws:secretsmanager:us-east-1:123:secret:worker-enroll",
)
pool = PoolDeclaration(
    name="workers", owner="platform", purpose="garden-worker", provider="ec2",
    profile=profile,
    provider_options={
        "instance_type": "m6i.xlarge", "subnet_id": "subnet-private",
        "security_group_ids": ["sg-egress-only"],
        "instance_profile_arn": "arn:aws:iam::123:instance-profile/scoped-worker",
        "hourly_usd": 0.25,
        "bootstrap_path": "/opt/company/bootstrap-v1",  # present on this pinned AMI
    },
)
lifecycle = HostLifecycle({"ec2": EC2Provider(workplace_ec2_client)}, state, workplace_policy)
print(lifecycle.plan(pool))       # read-only, disabled, desired=0
lifecycle.reconcile(replace(pool, enabled=True, desired=1))
```

The AMI must implement the [prebuilt bootstrap contract](host-bootstrap-contract.md);
a stock OS image is insufficient. The provider invokes the verified image executable and
does not assume an enrollment API exists.

The security group needs outbound HTTPS, not inbound SSH. User-data contains only the pinned
version, endpoint and a secret *reference*. The scoped instance role retrieves enrollment at
runtime; controller credentials and secret values do not enter tags, user-data or host facts.
IMDSv2 is required with a hop limit of one. Keep the worker process in a container/network
boundary that cannot access instance metadata, and grant the instance role only enrollment
and workload-scoped retrieval—not controller EC2 management. The consumer's health callback
must confirm HTTPS reachability and worker registration before the host becomes ready.

Set `GARDEN_EC2_CANARY=1` only in a separately budgeted acceptance harness which constructs
the workplace client and policy, records the reviewed plan, then enables one host. The harness
must retire it in `finally`, inventory owned instances, volumes and public IPs, and report any
retained billed resource and cost. Unit fakes intentionally do not claim this live evidence.

## Minimal second provider and development consumer

An extension implements the `HostProvider` protocol; no registration table or scheduler edit
is needed. `FakeProvider` is the complete local example:

```python
provider = FakeProvider()
dev = PoolDeclaration(
    name="alice-dev", owner="workplace", purpose="development", provider="fake",
    profile=replace(profile, name="remote-dev", persistent_workspace=True,
                    endpoint="", enrollment_secret_ref=""),
    enabled=True, desired=1,
)
dev_hosts = HostLifecycle({"fake": provider}, JsonStateStore(Path("hosts.json"))).reconcile(dev)
```

This imports no scheduler and starts no garden worker. Reducing desired capacity terminates
compute but reports the retained disk; deleting persistent workspace is a separate, explicit
consumer release decision. Connection details and active-session detection belong in the
workplace policy/profile integration. Selecting a development profile never creates SSH rules.

Contract additions remain backward compatible within `garden.hosts/v1`. A provider declaring
a different contract version is rejected before planning. Breaking field semantics require a
new contract version and an explicit adapter.

## Command-backed acquisition and warm reuse

`CommandProvider` adapts an approved controller-side wrapper without embedding a vendor or a
second infrastructure lifecycle. Configure `provider_options.command` as an argv list and an
optional `timeout_seconds` (at most one hour). The adapter appends one action argument and
writes one compact JSON request to stdin; the wrapper returns one JSON value on stdout. There
is no shell interpolation. Exit status, stdout and stderr remain byte-exact in
`CommandResult`, and every invocation is bounded. A wrapper can use an API, a queue, SSH, or
another approved transport, but it is always launched by the controller and does not require
one acquired host to connect to another.

The actions are `inspect`, `inspect-one`, `acquire`, `ready`, `release`, `start`, and `retire`.
`acquire` receives the stable logical host and operation aliases and may return
`provisioning`; later reconciliation discovers the same operation rather than submitting a
duplicate. `ready` is explicitly read-only and receives the configured workspace, exact
revision and harness. Its response has five booleans: `workspace`, `revision`, `provisioned`,
`harness_login`, and `smoke_probe`. The wrapper owns checkout reconciliation, but the
lifecycle will not lease the host unless all five checks pass.

`HostLifecycle.acquire_ready()` stores leases in the same atomic JSON state as pool facts.
It reuses a warm host only after the prior run is terminal, retires hosts beyond
`maximum_age_minutes`, and raises `EnvironmentStop` when preparation or readiness cannot
admit work. Consumers call it before counting or dispatching a task attempt. After dispatch,
`attach_run()` records the process identity. `release()` stops a reusable host and clears its
lease; `cancel_acquisition()` clears an unlaunched reservation, `orphaned()` exposes terminal
process leases for inspection, and `destroy()` remains the explicit retirement boundary.
