# How the scheduler talks to a worker

The scheduler is a Python function that runs for a few seconds every `tick_interval`.
A worker is an agent CLI that runs for minutes, started by the scheduler but not
attached to it. This page walks through everything that passes between them.

## The short version

Local and SSH-driven workers use the filesystem channels below. A pull-based `remote`
runner instead uses HTTPS and shares no filesystem with the scheduler.

| direction | channel | carries |
|---|---|---|
| scheduler to worker | the worker's **stdin** | the brief, once, at start (`runs/<task>/<run>/brief.md`) |
| scheduler to worker | the **working directory** | a git worktree on the task's branch, based on the right base |
| scheduler to worker | two **environment variables** | `GARDEN_TASK_ID`, `GARDEN_RUN_ID` (informational) |
| worker to scheduler | **stdout** | the harness's structured output: the final message, token usage, cost, session id |
| worker to scheduler | the **worktree** | commits on the task branch; publication is handled by the configured transport |
| worker to scheduler | one **file**, `exit_code` | the completion signal |

## Concurrent review and CI feedback

A revision waits for already-running review and CI analysis to finish. Their feedback is
stored separately for the target PR head and combined into the worker's saved brief, so a
late CI result cannot replace review findings or vice versa. A fresh review replaces its
own contribution; recovered CI clears only CI feedback. Operator-edited handoffs remain
intact, and GitHub comments retain their original commit context when available.

CI analysis records its target head before starting. Results for a moved head or closed PR
are retained as run evidence but cannot queue feedback for the new revision. Replaying a
collected CI result after a restart does not create another revision or duplicate comments.
Original verdicts, finding identities and repeated-finding stops remain part of the record.
## Workload identity at operation boundaries

`workload_identity.providers` and `workload_identity.references` are trusted local
configuration and part of the executable configuration fence. Provider adapters declare
interface version 1 and an exact capability set. A reference fixes its provider, operation,
audience, maximum scopes, maximum lifetime, named delivery target, and either environment or
HTTP-header bindings. `workload_identity.boundaries` selects the logical reference and bounded
request for a host-owned target such as `worker`; repository or claim data cannot select it.
Runtime code calls `WorkloadIdentityResolver.resolve` with the logical reference, operation,
audience, automation run identity, requested lifetime, and optional narrower scopes.

The local supervisor and managed remote consumer resolve the `worker` boundary immediately before
launching their supervised subprocess, using `automation:<run id>` as membership. The returned
object's public metadata contains only issuer, expiry, audience, scopes,
provider, and automation identity. Its secret values can be applied only through the
configured delivery method to the named subprocess environment or protocol request headers;
they are excluded from representations and cleared when the operation context closes.
Resolution, membership, expiry, renewal, validation, revocation, provider availability, and
policy mismatches raise `WorkloadIdentityError`. Callers classify that as an environment
failure and must not retry as an author revision or fall back to ambient credentials. Remote
hosts construct the same resolver from host-local trusted configuration; claims, briefs,
transcripts, and finish payloads carry neither provider registration nor authority values.
While the subprocess runs, its owner validates the authority and renews it when the provider
allows. Because a process environment cannot be changed after launch, a renewal that rotates an
environment-delivered value terminates the process and reports an actionable workload-identity
environment failure; a stable renewal may continue. Revocation and failed renewal do the same.
The local supervisor pipes stdout and stderr through a split-safe streaming redactor before
writing live run files, and publishes a redacted final message only after the raw harness file
has been removed. The remote consumer applies the same filter before heartbeat transcript and
finish requests.

## Independent hosts

With `runner: remote`, dispatch queues a run without launching a process. An independent
host runs `garden worker --garden https://garden.example --host build-1` and authenticates
with the bearer token named by `workers.hosts[].token_env`.

- `POST /api/runs/claim` leases one compatible work, review, persona, or check run and
  returns its brief, mode, branch/base, repository URL, setup timeout, turn cap, and
  environment-variable allowlist.
  Managed workers give each idle poll a random `claim_request_id` and retain it across
  transport and retryable HTTP failures. The controller durably stores that identity with
  the exact allocated lease response. A replay by the same host returns that response only
  while its lease generation is still current and within its recovery deadline; completion,
  expiry, host mismatch, or generation replacement returns a conflict. Backoff is capped at
  five seconds, while authentication, validation, and other permanent responses remain
  visible failures. Older workers without an identity remain accepted but cannot replay an
  ambiguously lost response.
- `POST /api/runs/<id>/heartbeat` renews the lease and appends transcript chunks. Claim
  returns a unique `lease_token`; every heartbeat and finish must echo it, so a worker from
  an expired claim cannot affect a run after it has been reclaimed, even on the same host.
- The worker pushes its commit to the claim's lease-specific staging ref, never directly to
  the task branch. `POST /api/runs/<id>/finish` records the exit code, final message, result,
  usage, cost, and pushed commit. The scheduler verifies that staged head and promotes it to
  the task branch with a git lease, then uses its ordinary
  result, PR, review, check, and accounting paths.
  Before posting, the worker durably saves the finish payload. After a process restart it
  retries transient delivery for a finite configurable window with capped jittered backoff,
  then continues serving while retaining the payload for a later attempt. Authentication,
  validation, and replaced-generation responses move the payload to a local quarantine and
  emit an operator-action event, so a supervisor cannot restart-loop on an undeliverable
  result. An already accepted identical finish remains an idempotent success.

Expired leases are claimable again and do not fail the task. Each reclaim gets a different
staging ref, so an expired worker that finishes cloning, setup, checks, or execution late can
only update its abandoned ref; it cannot overwrite the task branch. Browser origin checking still
applies; only a correctly token-authenticated runs API request bypasses it. Claim responses
contain no token or environment value. Repository URL user-info, query strings, and fragments
are stripped. Of SCP-style remotes, only the conventional `git@host:path` form is accepted;
other user identities and malformed URL-like remotes fail closed. Configured harness arguments
are not transported because they may contain inline credentials. The product's trusted
`setup.command` and timeout are transported so a managed consumer can prepare every execution
mode inside its admitted host slot; `setup.env` values are not transported. Git, setup, and
harness credentials belong to the host. A standalone `garden worker` continues to use
`--setup-command ...` for host-owned preparation. That explicit command also overrides
product setup for checks. The configured command is sent verbatim to the authenticated host;
keep credentials in host-local environment/configuration, never inline in that command.

```yaml
runner: remote
workers:
  lease_seconds: 120
  hosts:
    - name: build-1
      token_env: GARDEN_BUILD_1_TOKEN
      max_parallel: 2
web:
  # Required when the listener is not loopback-only. Keep it distinct from worker tokens.
  operator_token_env: GARDEN_OPERATOR_TOKEN
```

The worker endpoints and operator surface are separate authorization classes. Only
`POST /api/runs/claim`, `POST /api/runs/<id>/heartbeat`, and
`POST /api/runs/<id>/finish` accept worker credentials; worker tokens do not authenticate
pages, read APIs, configuration, decisions, maintenance, merges, or other controls.
Operator routes on a non-loopback bind require `Authorization: Bearer <operator token>`.
Set `web.worker_ingress: true` when a nominally loopback bind is deliberately published or
tunnelled to workers; this enables the same requirement. Startup refuses either exposure
when `web.operator_token_env` is absent or its environment variable is empty.

The application does not trust `Forwarded` or `X-Forwarded-*` for authentication or for
deciding whether a request is local. A reverse proxy may terminate TLS and inject the
operator Authorization header, but must strip client-supplied Authorization itself and be
the only network peer able to reach the garden listener. Browser mutations retain the
independent `Origin`/`Referer` allowlist check. Headerless direct requests therefore cannot
bypass operator authentication on an exposed listener. The default `127.0.0.1` bind is the
explicit development mode and leaves operator routes locally accessible.

`garden worker --garden URL --host build-1 --doctor --repo REPO --harness claude` checks
the token, git access, and harness. `--once` claims at most one run for CI-style hosts.
The standalone command uses the same durable worker identity, bounded event history, active
supervisor recovery, and pending-result delivery as a managed worker before it requests new
work, so restarting either daemon does not replay an accepted execution.
Before either a harness or remote check starts, its detached supervisor waits on a parent-owned
launch gate. The parent fsyncs the PID/birth-fenced active-claim handoff before releasing that
gate. A crash before persistence therefore starts no workload; a crash after persistence leaves
a handoff that the replacement daemon can collect, without issuing the workload again.

The worker's final message ends with one line, `GARDEN_RESULT: {...}`, and that line is
the whole result contract. For the local runner, publication of the task branch, pull request
and review comments is done by the scheduler. A product may explicitly set
`products.<name>.setup.worker_push: true` for a local worker that must publish its assigned
branch for CI; this does not grant PR-management credentials. The SSH runner pushes its
host-side branch, while the remote runner pushes a lease-specific staging ref for scheduler
promotion. Review comments and pull requests remain scheduler-owned.

## Review evidence

A task can explicitly ask the scheduler to produce review evidence with `requires:` frontmatter,
or with the same concise phrases in an acceptance criterion. These declarations are useful when
the evidence is material to the outcome; they are not a universal checklist inferred from a path
or keyword. For example:

```yaml
requires:
  - persona-review -p designer
  - persona-review -p usability-expert
  - captures
  - check: unit
```

`persona-review -p <name>` starts that PR persona review when the PR opens. `captures`
adds the UI check even when the diff did not itself select it. `check: <name>` selects a
named `checks.pre_pr` entry; task files name configured checks and never inject commands.
Checks finish before the PR opens. Required personas post their comments before the
automated review is dispatched, and their state is shown on the task page. Failed required
checks enter the normal mechanical changes-requested path with their diagnostic in the
revise brief.

When a task materially changes rendered behavior, its frontmatter declares that scope
explicitly instead of relying on prose or the path it edits:

```yaml
visual_scope:
  behavior: Tighter task-page spacing in the activity panel
  pages: [task]
```

`behavior` names what a person will see. `pages` is optional when the changed page module
identifies one affected page; shared styles without an explicit page list use representative
consumers. A task without this declaration has no screenshot requirement merely because it
edits a web module; its functional validation remains required.

Before dispatching a task that explicitly requires captures, the scheduler performs one
bounded Chromium launch in the product check's final scrubbed child environment. A failed
probe holds only capture-dependent tasks and is cached for five minutes; unrelated work
continues without consuming an attempt or starting a worker/base-probe run. Changes to the
relevant environment or `worker_env.pass` invalidate the cache immediately, and a successful
retry admits the preserved task through its normal dispatch path.

The diagnostic distinguishes missing Playwright setup, an absent configured browser
executable, missing shared libraries, a service-versus-child environment mismatch, and a
sandbox/launch failure. Install Playwright in the product's prepared check environment; in
an unprivileged environment, install browser/runtime files in a user-owned location and
expose only the necessary variable (commonly `LD_LIBRARY_PATH`) with `worker_env.pass`, or use product `setup.env`.
There is no universal library path, and the garden never installs host packages or grants
privileges. If Config shows `worker_env.pass` as held by an in-flight fence, accept the reload
through the supported Config action before expecting the probe to see it.

Browser readiness is infrastructure evidence only. It is not application acceptance. When a
task explicitly requires captures, the current PR head must produce the required output and the
result must be reported truthfully. HTML/text fallback output and partial screenshot sets are
diagnostics, not successful captures.

An owner can temporarily set `review.capture_infrastructure_policy: advisory` when the
screenshot host path is unavailable. The default is `require`. Advisory mode skips the
pre-dispatch browser hold but still runs the generated UI check. Its original failed result,
diagnostic, and any HTML/text artifacts remain in the check record; the scheduler separately
records that trusted browser-launch, capture-path, or clean-result-return failure as advisory
and can admit a review using focused behavior and functional evidence. The setting does not
cover an observed UI defect, an application or renderer traceback, incomplete interaction or
viewport behavior, another failed functional check, or source/artifact evidence that
contradicts the reviewed head. Those remain blocking, and a missing PNG is never reported as a
pass. Restore `require` once capture infrastructure is available.

The scheduler may classify web, lifecycle, Inbox/model-state, or QA changes as interaction
affecting and can request a disposable application replay. The reviewer chooses evidence
proportionate to the concrete behavior and reports the reviewed source and what was actually
observed. A running application, every empty/failure/recovery state, a prescribed JSON event
shape, and performance/load experiments are not blanket requirements. Performance or scalability
claims do require evidence that addresses those claims, and load work remains separately bounded
and opt-in. A stale-head result, observed defect, failed applicable check, or contradictory claim
remains blocking; absent optional evidence does not mechanically change an otherwise supported
approval.

## The sequence

```mermaid
sequenceDiagram
  autonumber
  participant S as scheduler (one tick)
  participant D as run directory
  participant T as worktree
  participant W as worker process
  participant G as GitHub
  S->>T: git worktree add garden/ID-slug from origin/main, or from a stack parent's branch
  S->>D: write run.json and brief.md
  S->>W: start, detached: cd into the worktree, run claude -p with brief.md on stdin, capture stdout.json and stderr.log, then write exit_code
  Note over S: the tick ends here and nothing stays open
  W->>D: read brief.md (stdin)
  W->>T: edit, run checks, commit (optional CI push)
  W->>D: final message ending in the GARDEN_RESULT line, plus usage and cost, into stdout.json
  W->>D: exit_code
  Note over S: a later tick
  S->>D: exit_code present, so parse stdout.json and keep final.md
  S->>T: local reap preserves uncommitted leftovers as a named recovery stash; SSH reap receives its host-side leftover commit; count committed work ahead of the base
  S->>G: push the branch, open a draft PR from pr_title and pr_body
  S->>W: start a review run the same way, with a review brief
  S->>D: run.json updated: status, usage, cost
```

## Step by step

### Local storage admission API

Local scheduler launches, setup, check/runtime scratch, harness probes, and checkout
materialization retain `resources.disk_reserve_bytes` of free space on every required
filesystem. Its default is 20 GiB (`20 * 1024**3`). On WSL this includes both the guest
filesystem and the Windows volume containing the distribution; set
`resources.windows_backing_path` when registry discovery is unavailable or the distribution
uses an operator-managed location. `resources.operation_required_bytes` supplies a
conservative per-operation estimate and is reserved across concurrent local runs.

External operator tooling can use `garden.storage.require_storage(paths,
reserve_bytes=..., required_bytes=..., windows_backing_path=..., operation=...)` before its
own staging. Arbitrary external scripts are not intercepted automatically. A failed or
unknown required measurement raises `StorageAdmissionError`; callers must leave work queued
and retry with bounded cadence. The scheduler records this as transient resource pressure
without changing operator or maintenance pauses, and never gates result collection.

### 1. Deciding what to run (scheduler, in `dispatch`)

Before anything is started, the scheduler settles every choice a worker might otherwise
have had to make:

- **Runner**: the task's `runner:`, else the product's, else the garden's (`local`,
  `ssh`, `remote` or `manual`).
- **Harness and model**: the task's `harness:`, else the product's, else the garden's;
  the model is the task's explicit `model:` or the harness's map from the task's
  `difficulty` (`easy`, `medium`, `hard`) to a model name.
- **Branch and base**: the branch is `garden/<id>-<slug>` (kept across runs). The base is
  the product's base branch, or, when stacking applies, the branch of the one dependency
  whose PR is still open.
- **Worktree**: the local runner creates `.garden/worktrees/<id>` from `origin/<base>`
  (fetched first) or reuses it if it already exists on that branch. The `ssh` runner
  creates or reuses its host-side worktree as described in its variant below. The pull-based
  remote runner creates or reuses its independent host-side clone and pushes through the
  lease-specific staging ref described above.
- **Paths in the brief** are relative to the worktree the worker starts in; the brief never names the garden's own checkout, so a worker has nowhere else to go.
- **The brief**: `build_brief()` assembles the operating rules, the principles digest, the
  product overview, the phase goals, the task body and the reading list (inlined when
  small, listed when large), plus the "Revision round" feedback for revise runs, the
  "Stacked branch" note for stacked runs, and every earlier question and answer.
  Reading-list snippets are read from the *target checkout* — the worktree above, which is
  prepared before the brief is built — so a file a dependency created or a stacked parent
  changed is inlined as the worker will see it, not from a stale base, and a path that
  truly does not exist there is listed as "not found when the brief was built".
- **The run record**: `RunStore.new_run()` creates `.garden/runs/<id>/<timestamp>-<mode>/`
  with `run.json` holding the choices above.

#### Controller restart recovery

Pull-based workers may keep their host-side clone, setup, model/check subprocess, transcript,
and completed result alive across an ordinary controller stop/start. Each claim has two
durable controller deadlines: `lease_expires_at` is the normal heartbeat deadline and
`recovery_expires_at` is the last instant that the same lease generation may reconnect.
The default recovery grace is 300 seconds after the 120-second lease (`workers.recovery_seconds`
and `workers.lease_seconds`). While that grace remains, the run is shown as reconnecting and
cannot be claimed by another worker. A successful heartbeat renews the ordinary lease and its
grace; it does not change any EC2/bootstrap runtime deadline.

Workers retry connection refusal, transport timeouts, HTTP 408/425/429, and 5xx responses with
bounded exponential backoff through the claim's total recovery window (the lease plus its
recovery grace, 420 seconds with the defaults). Each successful heartbeat starts a fresh total
window matching the controller's renewed durable deadlines. Authentication failures and other
4xx rejections are terminal.

Managed workers also keep a bounded, secret-free `worker-events.jsonl` in their work directory.
It survives daemon restarts and records UTC timestamps, a stable logical worker id, a new process
generation and increasing restart count, request/claim correlation, operation and endpoint class,
work state, HTTP status or exception class, retries/backoff, recovery outcome, and terminal reason.
The controller records request receipt and the corresponding accepted or rejected outcome in
`.garden/worker-events.jsonl` and
serves a bounded local-only view at `GET /api/worker-diagnostics`; inventory pages can consume that
API without contacting hosts. Request bodies, authorization values, private transcripts, and
physical provider identities are not recorded. A missing controller-side correlation means the
request did not reach this controller, not proof of which network component dropped it.

An idle claim uses a configurable finite `claim_recovery_seconds` window (300 seconds by default),
then exits with an operator action instead of retrying silently forever. The service supervisor may
restart it, producing a new process generation. A permanent authentication response exits without
retry. Before launching model work, managed workers write a mode-0600 active-claim handoff without
the brief or repository URL. Harness output is written to claim-scoped files and the execution
supervisor runs in its own session. A replacement daemon renews the same lease, waits for that
surviving supervisor, and collects and publishes its result before requesting more work. This does
not relaunch the harness. The handoff records the supervisor's process-birth identity as well as
its PID. Recovery verifies both before waiting or sending a signal; a missing or mismatched identity
is quarantined without touching that process, so PID reuse cannot transfer execution ownership.
Linux uses boot identity plus kernel process start ticks. On platforms where the worker cannot
obtain an equally strong incarnation identity, including macOS, an unfinished handoff fails
closed and is quarantined without signalling its recorded PID. A completed handoff remains
collectible from its durable exit record.
Completed finish payloads are written mode 0600 under
`pending-results/` before transmission;
on restart they are delivered before another claim and removed only after the controller's
idempotent acknowledgement. A replaced lease rejects the saved generation and never re-executes it.
When the durable recovery deadline passes, the old token is rejected and the run becomes
claimable with a new token and staging ref. Thus a stale generation can neither renew itself
nor publish after confirmed replacement.

Transcript heartbeats carry a byte offset. The controller appends only at its durable offset,
accepts an exact replay after an acknowledgement was lost, and rejects gaps or conflicting
bytes. A finish replay with the same lease, exit code, result, and pushed head is acknowledged
without collecting again; a conflicting replay is rejected. The scheduler alone promotes the
accepted staging ref and opens the PR, so reconnect does not repeat model work or publication.

Deploy the controller before workers when introducing this protocol version. New controllers
accept transcript heartbeats from older workers, but old controllers do not provide durable
offset acknowledgements or idempotent finish replay. Recovery is intentionally bounded by the
configured grace: outages longer than it cause reassignment, and the original subprocess must
stop when its next authority check is rejected. Set the grace below the independently managed
host runtime remaining at dispatch; the protocol never prolongs a host deadline.

### 2. Starting the process (the runner, in `start`)

Before potentially slow worktree or setup work, dispatch writes the run identity as
`requested`, then advances it to `preparing`. Only recording the detached worker PID
advances it to `running`; terminal outcomes are presented as `finished` by the operation
API. All three startup states reserve the task and capacity, so a timed-out retry cannot
start another run. After restart, a `requested` or `preparing` record with no PID is not
live. Ordinary launches are closed once and left retryable. Recovery API launches retain
their client idempotency key and are resumed on the same record when that client replays
after restart; the runner's setup lock/stamp makes an escaped setup child and the resumed
server reconcile preparation once. The preparation server's PID is never exposed as the
worker PID.

The local runner writes the brief to `brief.md` in the run directory and starts a small
supervisor, detached in its own session (`start_new_session=True`, stdin closed), so it
survives the scheduler exiting. The workload gets a second, separate process group. That
lets the supervisor wait for and signal the group after its shell leader exits on Linux and
macOS, without signalling itself. At stop or timeout it snapshots session-escaping descendants
before signalling their parents, so Darwin cannot reparent them before they are found. On Linux
the supervisor additionally becomes a child subreaper and adopts a deliberately daemonized
process after its parent exits. Darwin has no equivalent kernel facility: a command must not
double-fork or call `setsid()` and then let its parent exit. Garden disables Git's filesystem
monitor in worker checkouts because that optional daemon violates this contract and provides no
benefit to short-lived worker Git commands.

The supervisor owns the monotonic worker deadline itself; local execution does not depend on
the optional GNU `timeout` command. It writes `exit_code` only after the harness and every
process it still owns exit, and forwards a stop to the entire tree.
The supervised command is equivalent to:

```sh
GARDEN_EXECUTION_TIMEOUT_SECONDS=5400 GARDEN_EXECUTION_TIMEOUT_KIND=worker \
python -m garden.run_supervisor /garden/.garden/runs/WID-003/20260904T120000Z-work \
  "cd /garden/.garden/worktrees/WID-003 && \
  claude -p --output-format json --model sonnet \
    --permission-mode acceptEdits --allowedTools Bash,Read,Edit,Write,Glob,Grep,MultiEdit \
    'Carry out the brief that follows. It is the complete specification of your job.' \
  < /garden/.garden/runs/WID-003/20260904T120000Z-work/brief.md \
  > /garden/.garden/runs/WID-003/20260904T120000Z-work/stdout.json \
  2> /garden/.garden/runs/WID-003/20260904T120000Z-work/stderr.log"
```

The exact command is saved as `command.txt` next to the brief. The environment is
**scrubbed**, not inherited: of the scheduler's variables the worker (and the product's
`setup.command`, which runs in the worktree first) keeps only an allowlist
(`runner.base.PASS_ENV`: `PATH`, the locale, proxy and CA settings, and the
harness's own `ANTHROPIC_*`/`CLAUDE_*`/`OPENAI_*`/`CODEX_*`), plus whatever
`worker_env.pass` in `garden.yaml` names, plus the product's `setup.env`; then
`GARDEN_TASK_ID`, `GARDEN_RUN_ID` and the `GARDEN_ROOT` sentinel are set. No GitHub
token, cloud credential or ssh agent reaches the worker: it commits in its worktree and
the scheduler pushes. **`HOME` is *not* on the allowlist**: the worker runs under an
isolated scratch home beside its worktree (`.garden-home-<id>`), not the operator's, so it
cannot read the gh token, git credentials or ssh keys out of `~` (merely unsetting `HOME`
would not do — glibc and `expanduser` fall back to the passwd entry, the operator's real
home, so it is *set* to somewhere empty). A tool that genuinely needs the operator's home
gets it back with `worker_env.pass: [HOME]`. (`CLAUDECODE` is dropped so a garden can be
driven from inside another Claude Code session.) The shell's pid is stored in `run.json`.
For Codex the inner command is `codex exec --json --skip-git-repo-check
--full-auto -m <model> --output-last-message final.md -`, with the same wrapper; a custom
harness is whatever `command:` template the config gives, with `{model}` and `{final}`
filled in.

An isolated `HOME` would also hide each harness's own saved login: claude keeps
`.credentials.json` under `CLAUDE_CONFIG_DIR` (default `~/.claude`) and codex keeps its
login in `CODEX_HOME` (default `~/.codex`). Each dispatch builds fresh directories below
the scratch home, copying only those credential files from the operator's default locations
or `worker_env.config_dirs` sources in `garden.yaml` (e.g.
`{CLAUDE_CONFIG_DIR: /srv/claude-creds}`). Settings and instructions never carry over.
A custom harness reads whatever variable its own CLI defines; name that variable under
`worker_env.config_dirs` too. `garden doctor` checks each configured harness is actually
logged in through this exact scrubbed environment (`Harness.check_login`): a trivial
one-line prompt, not an ambient "auth status" call, so the check fails the same way a real
dispatch would if the private HOME hid the credentials. `Harness.parse` tags an output
that looks like a login failure (`"not logged in"`) with `env_error: true, env_kind:
"auth"`, so this is told apart from a worker's own failure and pauses the harness (an
environment stop) rather than counting toward the task's attempts.

Tools outside the harness can receive individual approved configuration files through
`worker_env.config_files`. Each entry is named and supplies a host-local `source`, a
`destination` relative to the isolated HOME, and optional `required: true`. The local, SSH,
and pull-based remote paths refresh only these files for each run, remove a stale optional
copy when its source disappears, and reject traversal or destination symlinks. Directories
are mode 0700 and files mode 0600. File contents never enter the brief or remote claim.

`start` returns at once. The scheduler records the `running` transition, bumps
`attempts` and `last_dispatched_at` on the task file, saves `state.json`, and the tick
moves on.

### 2a. The worktree fence

A worker may edit and commit only inside its own worktree. That boundary is the
**runner's**, not the brief's: it holds whatever the brief says, whatever a worker reads
on disk, and whatever a person types into an answer. Nothing a worker is told can lift it.

It is enforced in two layers:

- **First line — the harness denies the write.** For `claude`, the runner passes
  `--settings` with `permissions.deny` rules for the live garden and the product clone
  (and, when the harness config sets `sandbox: true`, an OS-level sandbox that confines
  every process's writes to the worktree — opt-in and untested, see the note below). Deny
  rules are evaluated before the `acceptEdits` mode, so an edit *inside* the worktree still
  needs no prompt while an edit *outside* it is refused — and in `-p` mode there is no one
  to approve a prompt, so a refused edit simply fails. The forbidden directories travel on
  the run as `fence_paths`, set at dispatch.
- **The garden's own config and side-store are hash-checked too.** The two guarded repos
  are git checkouts, but the garden's `garden*.yaml` (config) and `.garden/state.json` (the
  scheduler's side-store, where an approve verdict lives) are gitignored or otherwise the
  scheduler's own, so the HEAD/working-tree snapshot above would miss a worker writing to
  them. At dispatch the fence also hashes each of these files into the run directory (keeping
  a copy of the config files). On reap, a hash that changed *and appears in explicit
  structured write evidence* is an escape: a `garden*.yaml` is restored from its snapshot; `state.json`
  is the scheduler's to rewrite every tick and is not reverted, but the run still fails and
  the Inbox card names it for a person to inspect. A change the worker did not name is the
  scheduler's own `state.json` write, or an operator editing config by hand — left alone.
  This is what stops a worker forging its own approve verdict into `state.json` and, with
  automerge on, self-merging.
- **Belt and braces — the runner reverts what the worker itself wrote.** At dispatch the
  scheduler snapshots the HEAD and working tree of the live garden and the product clone.
  On reap, `finalize` compares them and reverts a change *only when structured harness
  output provides explicit write evidence for the path*. An attributed write is reverted
  (commits dropped with a soft reset
  that preserves unrelated in-flight edits, files restored or removed) and the run is marked
  **failed** with a card in the Inbox quoting exactly what was touched. Everything else
  outside the worktree is *left in place*: task files and `.garden/` are the scheduler's
  own; a config file a person edited by hand while the run was live, or a HEAD the
  scheduler's own `git fetch` advanced, is not the worker's and must not be reverted (a
  moved HEAD alone is not an escape). Such un-attributed changes are noted on the card for a
  person to check, never undone. A person answers the card; the answer cannot un-fail the
  run or reach back into the garden.

Attribution reads structured harness events, not arbitrary transcript text. Claude
Edit/Write tools, Codex file-change events, and shell commands with an evident mutating
operation or unquoted output redirect count as write evidence. Tool results, read-only
commands and final prose do not. Opaque commands whose effects cannot be established remain
ambiguous rather than becoming restoration authority. Mutable stdout, stderr and run audit
evidence owned by a concurrent worker is never restored from a dispatch snapshot: an explicit
forbidden write still fails and is reported, but the latest bytes remain available for
containment and incident recovery.

> The `sandbox: true` block is opt-in and has not been exercised against a real harness. It
> emits an OS-level sandbox stanza (`filesystem.allowWrite` = the worktree and `$TMPDIR`,
> `denyWrite` = everything else) that a given `claude` build must actually support; before
> flipping it, confirm the installed CLI honours `--settings` sandbox config on the host OS.
> Until then the deny rules and the belt-and-braces revert are the fence.

This closes the hole CG-054 and CG-058 left: those keep the brief and `garden` commands
away from the live garden; the fence keeps a worker's *writes* away from it even when the
brief never named it and a person told the worker to go there.

### 3. While the worker runs

Nothing is connected. The scheduler may not even be running: `garden tick` from cron
exits, `garden serve` may be restarted, the laptop may sleep. The run's existence is the
`run.json` with `status: running`, and its liveness is checked on demand:

- `exit_code` exists: the process finished.
- otherwise the pid and process group are probed (`kill -0`, with procfs on Linux and BSD
  `ps` elsewhere to exclude zombies); a pid that is
  gone means the process died without the wrapper writing the file (a hard kill, a
  reboot), which the scheduler treats as a failed run.
- a run older than `timeout_minutes` + 5 is killed by process group and marked
  `timeout`; the local supervisor's monotonic deadline is the first line of defence at
  exactly `timeout_minutes`.
- a run that has produced no output and touched no file in its worktree for
  `idle_minutes` is shown as "idle N min" on the running card; past `idle_kill_minutes`
  it is killed by process group and marked `timeout`, so a worker gone silent is stopped
  well before `timeout_minutes`. "Activity" is the newest mtime under the worktree (its
  `.git` aside) and the growth of the run's `stdout.json`/`stderr.log`; `idle_kill_minutes: 0`
  disables the stop.

For a pull-based remote run, both execution age and idle age begin at the first
`execution_started_at` claim (`claimed_at` is the compatibility fallback for older records),
never at queue entry. The controller checkout is not the remote execution checkout, so its
mtime is excluded; only output received by the controller advances remote activity. Lease
heartbeats retain and fence the claim but do not count as productive activity, which means a
claimed remote process with no output remains subject to the idle limit and the independent
overall execution deadline. An unclaimed queued record has no execution-idle age.

The web UI's "Running now" list and `garden runs` read the same `run.json` files. The
worker, meanwhile, sees a normal repository checkout on a branch and a prompt that ends
with the operating rules: commit in small steps, do not open a PR, do not edit `tasks/`,
run the project's checks, and finish with the result line. Local workers normally leave
branch publication to the scheduler; an explicitly configured `setup.worker_push: true`
may permit the assigned-branch CI push. SSH and remote workers follow their transport's
push rules above.

### 4. What the worker sends back

The last line of the worker's final message is the contract:

```
GARDEN_RESULT: {"status": "done" | "needs_input" | "blocked" | "wont_do" | "no_change",
                "summary": "1-3 sentences", "question": "only for needs_input",
                "reason": "only for wont_do / no_change",
                "pr_title": "...", "pr_body": "markdown", "pr_comment": "optional",
                "verified": [{"criterion", "evidence"} | {"criterion", "not_done", "reason"}],
                "pre_flight": [{"item", "status", "evidence"}],
                "friction": ["short item"], "notes": "...",
                "discovered": [{"kind", "title", "body", "file", "error", "difficulty", "blocking"}]}
```

- `done`: the branch is ready; `pr_title` and `pr_body` are used verbatim.
- For local runs, the scheduler pushes only committed work. Uncommitted files at dispatch or
  reap are preserved as a named recovery stash in the task worktree, with the task and run
  recorded in the run record and task state. Restore one with its recorded `git stash apply
  <sha>`; a later reap or revise starts clean and cannot add that artifact to the PR. The SSH
  host commits dirty paths before pushing instead; see its transport variant below.
- `pr_body` is the permanent description of the change for a reader without the task file:
  what it does, why, how it was verified, follow-ups. It never narrates the process — rounds,
  rebases, reviews, checks, prior attempts — and on a revise round it is omitted unless the
  description itself must change (the current one then stays). Process narration goes in
  `pr_comment`, posted as a PR comment.
- `verified` speaks to each acceptance criterion by name: one entry per criterion, in order,
  with `evidence` (the test that proves it, the command and its output, or the page and what
  it shows), or `not_done` with a `reason`. The scheduler builds the PR body's `## Verification`
  section from this list (`garden.criteria`), so the worker does not write one itself; the
  automated review is shown the same list to check each claim against the diff, and
  `garden metrics` reports criteria met on the first review per tier. A criterion with no
  evidence is a finding, not a pass.
- `pre_flight` has one row for every item in the review rubric the brief gives the worker:
  criterion evidence, lint, conflict markers, UI captures where relevant, PR description, and
  criteria-by-name. Each row says `pass`, `not_applicable`, or `fail` and gives short evidence.
  A missing row is mechanically sent back before a PR opens; conflict markers, Python syntax,
  missing UI PNGs for a UI diff, and an empty initial description are token-free pre-PR failures.
- `friction` is a list of short items (missing context, a confusing spec, tooling pain). The
  scheduler posts them as one marked PR comment and appends them to the phase's friction
  record; `garden friction` harvests them for the next planning round. Friction never goes in
  `pr_body`.
- `needs_input`: the worker committed what it had and stopped on a decision only a person
  can make; `question` is the one thing it needs.
- `blocked`: it cannot proceed at all; the task fails with the reason in its log.
- `wont_do`: the worker judges the task should not be done; `reason` says why. Not a failure:
  the task moves to `waiting_human` and the person accepts (it ends in the terminal `wont_do`
  status and any open PR is closed with the reason) or rejects (the reasoning goes back into a
  revise round with the person's note).
- `no_change`: a revise round found nothing to change (e.g. the failing check was the
  environment, not the diff); `reason` says why. The scheduler reconciles that claim against
  the unchanged head by running required checks and a fresh review. A finding that remains
  returns through the bounded revise loop. A person is asked only when the result explicitly
  leaves a criterion undone or declines an improvement, because that changes the promised
  product outcome rather than merely reporting evidence about it.
- `discovered`: things it noticed but did not do. Each item has a `kind` (default `task`):
  a `task` becomes a draft task file, unless its title (normalised) or its structured `file`
  and `error` fields already match an open task in this phase or the next one, in which case it is noted
  on that task ("also found by") instead of filing a near-duplicate, with a
  `discovered_duplicate` event; a `duplicate` (`of`/`duplicates`) or `cancel`
  (`task`) becomes a decision card for a human — Accept cancels the named task with the
  provenance in its log (and, for a `duplicate`, repoints any dependents onto the kept `of`
  task so they are not left blocked behind a cancelled one), Reject dismisses the card and
  logs the disagreement; a `note` (`note`) is filed to the phase's friction record and makes
  no card. Decision and note kinds never file work.

A `wont_do` or a `no_change` is a decision for the person, not a failure: the inbox card and
the task page quote the `reason` and show the worker's final message in full, with Accept and
Reject (plus a note). `garden accept ID` / `garden reject ID "note"` do the same from the CLI,
and `garden set-status ID wont_do --reason "…"` records a `wont_do` directly.

The harness wraps that message in its own format: `claude -p --output-format json` prints
one JSON object with `result` (the final text), `usage`, `total_cost_usd`, `session_id`
and `is_error`; `codex exec --json` prints JSON lines with `item.completed` messages and a
`turn.completed` usage record. `Harness.parse()` normalises both, and any plain-text CLI,
into `final_text`, `usage`, `cost_usd`, `session_id` and `error`; then `parse_result()`
scans the final text backwards for the marker and tolerates a fenced or trailing-junk
line by taking the outermost braces.

### 5. Reaping (scheduler, in `reap` and `finalize`)

On the next tick after `exit_code` appears, the scheduler:

1. Reads the exit code, calls the runner's `collect` (which calls `Harness.parse`), and
   writes the parsed result, usage, cost and session id back into `run.json`. It keeps
   the final message as `final.md` if the harness did not already write one. A
   `run_finished` event records the cost.
2. Decides from the exit code and the result line (the table is in
   `docs/architecture.md`): retry or fail, `waiting_human`, or carry on.
3. Files discovered work as task files in the same phase.
4. For a local run, preserves anything the worker left uncommitted as a named recovery
   stash, records its SHA and restore command in the run and task state, and keeps it out of
   the PR. A later local dispatch also stashes dirty leftovers before syncing a reused
   worktree. The SSH host instead stages and commits dirty paths as a synthetic leftover
   commit before pushing; SSH has no scheduler recovery-stash record. In either case, the
   scheduler counts committed work ahead of the base and fails the run if there are none.
5. Pushes the branch. From here on the branch exists outside the machine.
6. Runs `checks.pre_pr` in the worktree (tests, lint: no model). A failure becomes
   feedback and the task goes to `changes_requested` before any PR exists.
7. Opens the PR (draft by default, with a footer naming the task, any stack parent and
   discovered ids), or for a revise run updates the title and body of the existing PR
   and leaves a comment. The task moves to `awaiting_triage` or `in_review`.
8. Starts the automated review run, and any personas listed in `review.personas`.

The scheduler never reads the worker's stderr for anything but an error message when
there is no output, and never inspects the worker's transcript. The result line, the
commits and the harness's usage numbers are all it uses.

### 6. The review run answers the same way

A review is a worker with a different brief: the task brief without the operating rules,
the PR title and body, the diff against the base (inlined under `review.max_diff_chars`,
otherwise read from git in the worktree), and the author's per-criterion `verified` claims
under "Author's verification". It ends with `GARDEN_REVIEW: {"verdict", "summary",
"criteria": [{"criterion", "met", "evidence", "reason"}], "description_ok", "description_feedback",
"findings": [...]}`. `criteria` speaks to each acceptance criterion by name, checking the
author's evidence against the diff; a criterion with no evidence, or one the author marked
not done without a reason the reviewer accepts, is `met: false` and a blocking finding. The
scheduler mechanically changes the verdict to `request_changes` when any returned criterion
is unmet or lacks its own `evidence`, so an approving top-level verdict cannot bypass the gate.
Reaping it posts the verdict as a PR comment; `request_changes`
turns the blocking findings and the description feedback into the next revise brief.
Persona reviews (`GARDEN_PERSONA:`) and trial comparisons (`GARDEN_COMPARE:`) use the same
transport and their own marker.

### 7. Pausing on a question, and resuming

When a worker reports `needs_input`, the scheduler stores the question, the harness's
`session_id`, the host it ran on and the harness name, and moves the task to
`waiting_human`. The task holds no slot. The inbox, `garden inbox`, the TUI and `garden
digest` all show the question.

`garden answer WID-003 "SQLite, one file per import"` (or the inbox form) appends the pair
to the task's `qa` list and dispatches a `resume` run. With a harness that can resume
(`claude -p --resume <session>`, or `codex exec resume <id>` when enabled), the command is
the same as before except that the prompt on stdin is only the resume note: the question,
the answer, and a reminder of the rules and the result line. The session picks up where it
stopped, with its earlier context intact, in the same worktree. A harness that cannot
resume gets a fresh run whose brief carries every previous question and answer under
"Answers from the human". Resume runs do not count as attempts.

A resumed run is fenced exactly like a fresh one (see §2a): the answer becomes the prompt
on stdin, and no wording in it — "go fix it in the garden yourself" included — can let the
worker write outside its worktree. The runner denies the write, and `finalize` reverts and
fails the run if one slips through.

### 8. Revise runs

Feedback from the human's triage note, from review comments on GitHub, from a red CI
rollup, from the automated review or from a rebase conflict all land in the same place,
`pending_feedback` in `state.json`. The next dispatch starts a `revise` run: the same
worktree and branch, the same brief plus a "Revision round" section and the feedback
itself (only what is new since the last dispatch). The result line, push and PR update
follow the same path; the revise run's `pr_body` replaces the PR description. When the
automated review's only finding is the PR description (no blocking findings), the revise
round dispatches on the easy tier's model regardless of the task's own difficulty; any
blocking finding keeps the task's tier.

## Variants of the transport

**ssh runner.** Every SSH worker runs in its own detached tmux session on the selected
host. Remote hosts require Python 3 and tmux, in addition to Git, the harness and the
configured development environment. `ssh.python` can select the remote Python command.
This changes the SSH prerequisite; a missing tmux installation produces an explicit
preparation hold rather than falling back to a worker tied to the connection.

The scheduler transfers a trusted supervisor and `remote.sh` over `ssh <host> sh -s`.
The script refreshes the product clone, creates or reuses
`<repo>/.garden-worktrees/<id>`, and runs setup and the harness under the configured
worker environment policy. Successful runs commit remaining changes and push the assigned
branch. Failed or interrupted runs retain their dirty work and commits. Divergent remote
branches require deliberate recovery and are never reset to discard local commits.

The supervisor keeps logs, references and the exact terminal head under a private,
run-specific directory in the repository's common Git directory. An atomic checkout
lease prevents a second writer, including one launched by another controller. The lease
is released only after Garden has collected and acknowledged completion. The supervisor
enforces the worker timeout and stops its exact descendants before writing completion.
On Linux, a subreaper also owns descendants that detach with `setsid`; on other platforms,
cleanup covers the process group and descendants still linked by live parentage.

SSH connections only launch or inspect that durable run. A lost connection or a dead
local collector never counts as remote completion. The collector reconnects, resumes logs
at their byte offsets and adopts the same result. Credentials and the fresh remote login
environment cross a private, one-use pipe; neither is stored in the supervisor's files or
tmux arguments. Existing `worker_env.pass` and `setup.env` policies still apply.

`garden log TASK-ID` shows the logical host and a command like
`tmux attach-session -r -t garden-TASK-ID-<run-key>`. Connect to that host first, then run
the command to watch without sending keystrokes to the worker. Detach with **Ctrl-b d**.
The pane displays assistant text and tool activity while the complete raw stream remains
in the run logs. Finished sessions and run artifacts remain available for inspection;
remove only the named finished session when done, never the shared tmux server.

Recovery is bounded by `ssh.recovery_timeout_seconds` (default 300), with each SSH call
bounded by `ssh.connect_timeout_seconds` (30) and polls spaced by
`ssh.poll_interval_seconds` (2). A product's positive `timeout_minutes` is enforced on
the host; a zero setting uses the SSH runner's 90-minute safety bound. When liveness or
completion remains uncertain, Garden keeps checkout ownership and presents an operator
hold. `garden ssh-recover TASK-ID` grants another bounded collection attempt against the
same identity; it never starts another implementation. A definitive launch refusal can
be explicitly retried after its prerequisite or conflicting owner is resolved. Legacy
SSH runs without durable records require manual liveness verification after transport loss.

On reap the scheduler fetches the branch, requires commits ahead of base, and materialises
a local worktree for checks and review. The least-loaded host with a product clone and a
free `max_parallel` slot is chosen at dispatch; resumed work returns to its original host.

**manual runner.** A person is the worker. `garden take WID-003 --worktree` dispatches a
run with no pid, prints the brief path and creates the worktree; the `garden-take` skill
does this from inside an interactive Claude Code session. `garden finish WID-003 --result
'{...}'` writes `result.json` and `exit_code` into the run directory and calls the same
`finalize` as a detached run, so the push, checks, PR and review are identical. A manual
run never times out and never occupies a scheduler slot.

For work already implemented in an operator's checkout, claim its real identity instead:

```bash
garden take WID-003 --branch operator/fix --external-worktree /path/to/checkout
# commit, push and open the PR from that checkout
garden finish WID-003 --pr https://github.com/OWNER/REPO/pull/123 --summary '...'
```

The claim records the branch and PR rather than creating or inferring a garden worktree.
An already merged PR completes only after its head is verified on the final base; an open
PR retains its normal checks and review. If the external work cannot proceed before a PR,
use `garden finish WID-003 --blocked --summary '...'`; it follows ordinary manual blocked
handling. Git and fence protections remain in force in all three cases.

When the work was authored and pushed from a separate clone before a PR exists, use the
explicit pushed-result contract. The repository, already-claimed branch, and full SHA are
all required; the scheduler fetches the configured repository, requires that exact SHA at
the remote branch tip, materialises it locally, and only then runs the ordinary checks, PR,
and independent review path:

```bash
garden take WID-003 --branch operator/fix --pushed-result
garden finish WID-003 --repository OWNER/REPO --branch operator/fix \
  --pushed-sha 0123456789abcdef0123456789abcdef01234567 --summary '...'
```

A missing branch, repository mismatch, or stale SHA is refused without closing the manual
run, so corrected evidence can be submitted after a controller restart. The result summary
is provenance, not approval; checks and review remain authoritative.

**the planner.** `garden plan` (and the synchronous kickoff review it runs first) is the one
model call that is not detached: it runs the harness synchronously with the planning prompt
on stdin and imports the JSON array it prints as task files. Goals, specs and docs are
inlined into that prompt verbatim, so the call itself is isolated like a worker's rather than
run as the operator: a scratch directory (never the garden root), the scrubbed worker
environment (no operator token, no operator `HOME`), the live garden denied as a write
target, and `GARDEN_ROOT` forced to a sentinel (`planner.run_planner`). A generated task's
brief still goes through the same gate `garden approve` uses (`brief_gaps`) before it can
reach `ready`, whatever `plan.auto_approve` says.

## What each side never does

| the scheduler never | the worker never |
|---|---|
| calls a model, or reads a transcript | opens a PR or comments on one; a local worker normally leaves publication to the scheduler |
| holds a connection to a worker | edits files under `tasks/` |
| edits code in a worktree (local uncommitted leftovers are stashed, not committed; SSH commits its dirty paths on the host) | reads the whole garden; it gets the brief and the reading list |
| retries without a cap | waits for the scheduler; it finishes and exits |
| lets an answer or a brief widen the fence | writes or commits outside its own worktree (the runner denies it; a slip is reverted, §2a) |

## When things go wrong

| failure | what the scheduler sees | what it does |
|---|---|---|
| the harness crashes | non-zero `exit_code`, no result line | retry while `attempts < max_attempts`, then `failed` with the last stderr lines in the task log |
| the worker forgets the result line | exit 0, no `GARDEN_RESULT` | same as a crash; the final message is kept in `final.md` |
| the worker did nothing | `done` with zero commits ahead of the base | run failed; retry or fail |
| the worker runs too long | elapsed time past `timeout_minutes` + 5 | kills the process group, marks the run `timeout`, retries or fails |
| the worker goes silent | no output or worktree change for `idle_kill_minutes` | kills the process group, marks the run `timeout`, retries or fails (shown as "idle N min" from `idle_minutes`) |
| the machine rebooted mid-run | pid gone, no `exit_code` | treated as a finished run with no output: retry or fail |
| the push is rejected | git error after the commits were counted | `failed` with the error in the log; the commits stay in the worktree |
| GitHub is unreachable | `gh` and the token both unavailable, or the API errors | the task moves to `in_review` with a note to open the PR by hand and register it with `garden pr ID URL` |
| the answer arrives but the session is gone | `session_id` set, resume command fails or the harness cannot resume | a fresh run with the Q&A in its brief |
| two ticks overlap | both read the same `run.json` | the run is reaped by whichever finishes first; the second sees the run already marked done and finds no active run |
| the worker wrote outside its worktree | explicit structured write evidence names a changed path in the live garden or product clone | that write is reverted (commits soft-reset, files restored), the run is marked `failed`, and the Inbox shows a card quoting what was touched; mutable sibling run evidence is reported but never rewound, and ambiguous changes are left in place (§2a) |

## Where to look

- `garden log WID-003` prints the task's `## Log`, one line per transition, with cost.
- `garden runs WID-003` lists every run with mode, status, model, minutes, tokens and
  cost; the run directory holds the brief and raw output.
- `garden events WID-003` is the task's timeline from `events.jsonl`; the web task page
  shows the same with the last run's log.
- `garden brief WID-003 --stats` prints exactly what the next worker would receive and
  how big each section is.


## Proportionate verification and evidence metadata

Authors and reviewers receive the same verification guidance. Prove the requested
outcome and explicit constraints; implementation details and equivalent meaningful
checks may vary. Authors report source/command/result and artifact references in
`verified[].evidence`; reviewers inspect existing evidence and report their conclusions.
For served journeys, keep the actual actions and consequences in `interaction.events`
and point to saved evidence where available. Check paths and facts before finishing.

Missing metadata is an advisory, not another implementation round. A saved artifact
may use a different schema or wording from the reviewer report. The controller no
longer requires a JSON file whose states/events exactly equal the reviewer's paraphrase.
The old author brief exposed per-criterion prose while the detailed interaction fields
were only described to reviewers, and the review JSON example omitted the events it
required. Both briefs now state the evidence contract and the example includes events.

Actual failed checks, contradictory source identities, failed or materially unverified
journeys, and explicit phase evidence holds still block. An unavailable reference is
reported honestly; this policy does not manufacture a performed test or interaction.
Final current-head checks and CI remain required. Reviewers should reuse inspectable
passing evidence and identify the concrete defect or unmet outcome behind a send-back.

### What scopes a run's checks

Five things can speak to what a run validates, in this order:

1. **Owner direction.** An owner note (a triage reply, an inbox answer, a Config change)
   overrides everything below it for that run.
2. **Task criteria and `requires:`.** Acceptance criteria set the required outcomes; a
   task's `requires:` frontmatter (`persona-review`, `captures`, `check: <name>`, see
   above) adds specific evidence or a specific named `checks.pre_pr` entry on top of the
   product default.
3. **The frozen validation plan.** `dispatch` computes it once per run from the changed
   paths and the task, and it travels with the run (`env_snapshot.validation_plan`). It
   names the pages, interaction/scalability evidence, and checks this run requires; it does
   not expand later even if the diff or task body changes underneath it (see "criteria
   changed after dispatch" above for the criteria case).
4. **Product `checks.pre_pr`.** The command list the mechanical check runner actually
   executes for every run of that product, absent a narrower `requires: check:` selection.
5. **External CI.** Runs after the PR exists and is never a substitute for 1-4; a red run
   here still blocks independent of what the pre-PR checks reported.

The worker's brief and the reviewer's brief both render checks 2-4 as one instruction set,
computed from the same `checks.pre_pr` specs the mechanical runner uses — never a
separately-derived guess. Once a run's plan is frozen, a targeted selection is authoritative
for that run: the worker must not additionally run an unlisted broad or browser-backed
suite, and the reviewer may reuse those checks or add a narrower focused one but must not
broaden into a suite outside that recorded scope. Owner direction (1) is the only thing
that can widen it.

### Remote model validation supervision

Remote work, review and persona harnesses run through the same execution supervisor as
local harnesses. The host creates a private per-claim metadata directory and a fresh owner
identity; the supervisor supplies `GARDEN_VALIDATION_RUNNER` and `GARDEN_EXECUTION_RUN_DIR`.
Workers invoke direct pytest commands through
`"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- <pytest command>`. Opaque scripts and
build-tool launchers fail closed because they could discard the current selection policy before
delegating to an older pytest configuration; non-pytest tools run directly. Validation remains
serialized per owner and uses the host's heavy-work
admission and service limits. Controller paths and inherited execution ownership are not
forwarded as a substitute. The controller's `checks.timeout_seconds` value travels with a
remote claim and in the scrubbed local/SSH worker environment. Its hard clock starts only
after owner and host admission, remains fixed through primary-command execution and adopted
descendant drain, and never resets for output or retries. On expiry the supervisor records
`validation_timeout.json` plus `execution.json` state `timeout`, returns 124, terminates only
its owned descendants, and releases its validation slot while the parent model remains live.
Nonzero command results propagate through the wrapper.

### Runner-spawning test environments

Tests that start a real `LocalRunner` supervisor are synthetic child runs, not nested
validations of the test command. Build their `env` from the current environment after
removing `GARDEN_EXECUTION_RUN_DIR`, `GARDEN_EXECUTION_OWNER`,
`GARDEN_HEAVY_EXECUTION`, `GARDEN_OWNER_SCOPED`,
`GARDEN_EXECUTION_TIMEOUT_SECONDS`, and `GARDEN_VALIDATION_INHERITS_LEASE`. The child
supervisor then assigns its own owner and run directory and competes normally for the
authoritative host slot. This prevents a runner-spawning test executed through
`garden.validation` from waiting for the enclosing test command's owner lock.

Keep the child bounded and reap it in fixture cleanup. Production worker environments
continue to pass their execution identity to supported validation wrappers: the isolation
is only for disposable test-created supervisors.

Detached check claims use the same supervisor and capped post-admission clock around their
whole check batch. The validation deadline is replaced by the separately configured worker
deadline for ordinary local work, review, and persona runs, so the check budget never shortens
a model session.

This requires a versioned worker runtime update. Updating only the controller's briefs or
exporting the interpreter variable on its own does not repair an already running worker.
Stress/load experiments remain outside the ordinary suite and require separate opt-in.
