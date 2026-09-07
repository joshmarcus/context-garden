# Prebuilt environment bootstrap contract

An EC2 provider cannot invent an enrollment API or assume AWS CLI, Tailscale and the worker are installed. Endpoint-bearing profiles must specify `ec2.bootstrap_path`, an absolute executable on their pinned AMI. A stock Ubuntu AMI does not satisfy this contract. The provider fails before launch if the contract is unspecified.

Cloud-init invokes that executable with `--config /run/host-bootstrap.json`. The0600 JSON supplies contract_version, host, endpoint, secret_ref, profile_version and bootstrap_version. It contains references only, never keys. The executable must verify the installed image/profile versions, retrieve the scoped secret using its instance role, enroll the host network if required, and start the environment service. A nonzero exit is a bootstrap failure. EC2 running state is BOOTSTRAPPING, never READY; a consumer-injected health check must prove its actual service before readiness.

For a garden profile, the image must contain pinned worker, git, AWS-secret retrieval and Tailscale dependencies. Its bootstrap must start `garden worker` using the configured bearer-token protocol (claim/heartbeat/finish), not `/api/workers/enroll`. Run workloads unprivileged with disk temp and enforce separation from IMDS and enrollment material. The generic lifecycle neither transports controller credentials nor implements garden-specific authentication. CG346 owns this garden environment adapter and its image/host isolation validation.

For a development-host profile, supply a different executable that starts the development environment and retains workspace volumes. No scheduler imports or garden task IDs are required.

Deployment is deliberately fail-closed: the current stock Ubuntu canary plan cannot be enabled until a verified image implements this contract. The first real host still requires a scoped one-host deadline, $2 admission budget and actual retained-resource inventory. AWS budget reports and plan estimates are not hard billing caps.

Policy-required tags are injected into EC2Provider(required_tags=...). Reserved context-garden:* and aws:* tags cannot be overridden. Tags cover instance, volume and network-interface creation. Burstable instances default to standard credits. Destroy waits for observed termination and inventories real remaining EBS/ENI/address IDs; timeout returns DRAINING, never invented completion. A deleted instance can still have billable retained resources.

## Verified artifact alternative

A stock pinned AMI may use `bootstrap_url` plus `bootstrap_sha256` instead of a preinstalled executable. The URL must be credential-free HTTPS; EC2 userdata verifies the downloaded consumer executable before installation at `bootstrap_path`. Consumer dependency pins and verification remain the profile's responsibility. `shutdown_behavior: terminate` supports bounded trial deadlines without retaining a stopped billed disk.

The Ubuntu consumer is `scripts/managed-worker-bootstrap`. It obtains only the designated bootstrap secret using the instance role, installs the exact source commit, joins the scoped tailnet, blocks unprivileged IMDS access, and starts `garden.managed_worker` as an unprivileged system service. A process lock spans all execution stages and measured memory/disk admission precedes claims. Host facts accompany claims/heartbeats and are retained beside controller run artifacts. The live canary uses a deterministic test harness and a disposable Git remote; it does not prove model-account enrollment or real GitHub PR creation. External cleanup must be armed before provisioning; the in-instance shutdown timer is an additional safeguard, not sufficient by itself.
