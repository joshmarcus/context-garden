# Idle claim 502 and worker restart incident — 2026-09-10

This report links the observed runtime incident to the recovery and diagnostic behavior in
`remote_worker.py`, `managed_worker.py`, `worker_diagnostics.py`, and the worker protocol API.
It intentionally contains no host addresses, tokens, request bodies, or private transcripts.

## Finding

The bounded operational sample recorded 38 service restarts across six workers. Immediately
before the sampled exits, the workers were idle and repeatedly received `502 Bad Gateway` for
`POST /api/runs/claim`; no `401` or `403` response was observed. The 502 responses are the
observed trigger for these daemon exits. Because the workers were idle, the sampled incidents
did not interrupt an execution or result delivery and did not lose task work.

The confirmed source cause of the restart loop was the deployed worker's treatment of a
retryable claim response as an uncaught terminal error. The controller/proxy component that
generated the 502 is **unknown**: the retained evidence does not include a correlated proxy
request identifier, upstream status/exception, or supervisor exit record. In particular,
proxy, tailnet, and WSL origins remain hypotheses. The separate fresh-WSL command-launch hang
has not been correlated to this incident. A previous `timeout=60` classification was invalid
because it matched source text rather than a runtime exception.

## Source and deployed-runtime distinction

PR 394 / CG-491 is present in current source (merge `a6d5a8a9`). It already retries transient
idle claims with a stable claim identity, and the controller replays the same lease generation
after an ambiguous response. The observed RC16 installation predates that merge, so the
incident demonstrates a deployment gap rather than an unfixed current-source claim bug. No
live rollout, fleet restart, replacement, or runtime hotpatch was performed for this task.

## Corrective actions and next recurrence

Current source now also writes bounded JSONL transport and lifecycle events on both sides.
Records carry UTC time, stable logical worker identity, process generation and restart count,
request/claim correlation, endpoint class, operation, work state, status or exception class,
retry/backoff, recovery outcome, and an actionable terminal reason. Claim recovery has a
configurable finite window; permanent authentication responses stop immediately. The safe
`GET /api/worker-diagnostics` export reads controller-local history only and is suitable for
the existing worker inventory consumer without synchronous host fan-out.

At the next recurrence, correlate the worker `request_id` with the controller record. If the
worker has a transport exception and the controller has no matching receipt, classify it as
network-path loss. If the controller records a response, use its actual status; a 5xx without
an upstream diagnostic remains `controller_or_proxy`, not a guessed proxy cause. Process
generation and restart count distinguish a daemon reset from network recovery. Host termination
still requires an optional platform supervisor/provider event; where that collector is absent,
the cause must remain unknown. Rollout must use the normal validated release procedure.

## Limits of the historical evidence

The sample establishes the idle failure chain only. It does not prove every earlier disconnect
was idle, identify which upstream emitted the 502, or establish a host termination. Those claims
require the correlated records above plus, when available, a capability-checked supervisor or
provider event. The portable capture path is filesystem JSONL and uses no systemd dependency.
