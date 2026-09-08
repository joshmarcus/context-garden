# Resumable production worker scaling

`garden hosts scale POOL.json --deadline 2026-09-08T20:00:00Z` records one bounded
operation before it mutates AWS. The JSON is the versioned pool declaration documented in
`host-lifecycle.md`; use an enrollment reference ending in `{host_id}` so each stable slot
gets a separately revocable Secrets Manager secret. `--continue` resumes it, a repeated
request is idempotent, and `--cleanup` converges to zero through the same record.

The status output always includes desired, healthy, pending and failed counts; exact AMI,
profile and bootstrap versions; projected and admitted cost; the absolute deadline; missing
setup per host; retained resources; and the delayed-billing warning. The request is refused
when this operation plus sibling admitted operations exceeds `--aggregate-limit` (default
$80). Pool maximum and spend limits remain independently enforced by `HostLifecycle`.

## Enrollment boundary

Controller-owned `.garden/hosts/enrollment/HOST.json` files contain only non-secret
references and identity labels: `secret_ref`, `model_identity`, `repository_identity`,
`tailnet_identity`, `controller_identity`, and optionally `model_expires_at`. A missing or
expired item is printed as the next owner handoff and blocks only that new slot. Existing
healthy hosts keep running. Values and operator/root credentials must never be placed here.

Production integrations should mint AWS access with a scoped instance role (or Roles
Anywhere for an external controller), tag-limited single-use Tailscale OAuth credentials,
renewable GitHub App installation credentials or per-host repository keys, and a scoped
worker token. Codex account sessions may refresh on their host, but their eligibility must
be established by the account owner. Switching to an API key or enterprise workload
identity changes account eligibility and may create separate API billing; it is never an
automatic fallback.

## Readiness and recovery

The lifecycle health callback is the readiness gate. For production it must verify the
pinned bootstrap manifest, authenticated worker registration, repository and CI access,
and completion of a real portable-protocol task whose commit/result reached durable
controller storage. Only then may it return healthy and promote `bootstrapping` to `ready`.
Failed gates remain explicit and are cleaned up according to the pool retention policy;
successful siblings are retained. Stable operation tags prevent a restart, duplicate
request, or uncertain AWS response from creating another instance.

An absolute deadline is durable and is rechecked on every continuation. Once passed, the
operation converges to zero. Teardown inventories retained disks, interfaces and addresses;
credential references pending external revocation and delayed provider costs stay visible
instead of being reported as gone. The instance's systemd deadline is defense in depth.
