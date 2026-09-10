# Code simplification audit (CG-503)

Source inspected: `fc6c856f997c18342b77441368895ebd80549af8` (`garden/cg-503-find-removable-and-overengineered-code-to-simpli`).

Environment checked: WSL2 Linux 6.6, x86-64, using the prepared Python 3.14.4 environment.
No macOS or native Windows runtime was available. All proposed paths keep `pathlib`, the
existing GitHub provider interface, and configurable repository paths; none retires an
optional platform or provider.

## Scope and method

The audit followed the scheduler tick through task discovery and GitHub polling, then checked
the web request snapshot, CLI registration, check plugins, runner adapters, host enrollment,
and repository scripts against source, tests, and documentation. A small AST survey identified
large branch-heavy functions for manual review; low textual reference counts were not treated as
dead-code proof because Typer/FastAPI decorators, configured `module:function` check plugins, and
runner imports are dynamic entry points.

The representative high-complexity paths were `PollMixin.poll` and `_automerge_gate`,
`DispatchMixin._dispatch`, `ProductionEnrollmentResolver._ensure_locked`, `Store._scan`, and the
web `Hub.fresh` snapshot path. Dispatch and enrollment are long, but their apparent complexity
mostly encodes externally observable recovery ordering and least-privilege enrollment. They are
retention findings below, rather than invitations to flatten safeguards.

## Ranked candidates

### 1. Make routine PR feedback reads incremental

- **Purpose and callers:** `PollMixin.refresh_open_prs` lists every open PR and calls
  `GitHub.feedback_since(..., "")` for every PR on every scheduler tick
  (`src/garden/scheduler/poll.py:91-152`). The returned observations feed linked-task polling and
  the Board. `PollMixin._after_ci_check` repeats the same complete instruction read
  (`src/garden/scheduler/poll.py:436-439`).
- **Unnecessary cost:** `GitHub.feedback_since` accepts a timestamp but both paths pass the empty
  cursor. Each invocation issues three fully paginated reads—reviews, line comments, and issue
  comments—and only then filters/deduplicates in memory (`src/garden/github.py:662-736`). The
  observation already persists both `feedback_since` and stable `feedback_seen` identities
  (`src/garden/scheduler/poll.py:116-151`), so the timestamp is recorded but unused for fetching.
- **Measured evidence:** a provider-boundary experiment over 10 PRs with 100 historical reviews
  each observed 30 external `gh api --paginate` commands from feedback reads (plus the single
  open-PR listing in a real refresh). Local JSON/filter work was 1.82 ms, showing the relevant
  cost is the 30 provider processes/network traversals, not Python CPU. Page counts grow further
  with history. This is a call-count measurement; it does not claim production network latency.
- **Smaller alternative:** after the initial identity-seeding pass, query from a persisted cursor
  with a bounded overlap and keep stable-ID deduplication. Preserve the overlap because a strict
  timestamp cursor can miss two comments with the same timestamp—the reason documented beside
  the current code. Prefer provider-side `since` parameters where GitHub supports them; otherwise
  stop pagination once an ordered page is wholly older than the overlap. Keep complete feedback
  only for explicit investigation handoff (`complete_feedback`).
- **Benefit / risk / dependencies:** high recurring network and rate-limit benefit, with a smaller
  routine state transition; medium correctness risk around equal timestamps, edits, ordering,
  and controller restarts. It depends only on the existing GitHub abstraction. Bound the change
  to feedback retrieval and tests for same-timestamp comments, repeated pages, restarts, ignored
  authors/notices, provider failures, and the CI continuation.

### 2. Replace whole-tree per-request deep copies with copy-on-write task snapshots

- **Purpose and callers:** every web request receives an isolated `Store`; its first `Hub.fresh`
  call runs `Store.discovery_snapshot`, fingerprints discovery files, deep-copies every product,
  phase, and task, and rebuilds a task map (`src/garden/web/common.py:78-87, 244-260` and
  `src/garden/store.py:53-70`). Isolation protects a request from concurrent action mutations.
- **Unnecessary cost:** GET handlers overwhelmingly read models, yet they pay mutation isolation
  for the entire garden. On the disposable 600-task fixture, with `tracemalloc` enabled and 15
  samples, the median metadata fingerprint was 76.62 ms, warm snapshot 106.03 ms, and cold parse
  1019.48 ms; snapshot peak allocation was 2.67 MiB. The repository's server benchmark without
  allocation tracing (120 tasks, 120 runs, 500 events, two warm samples) showed caching reduced
  medians from 116.9-177.6 ms to 15.6-48.0 ms across representative pages, so the cache must be
  retained; this proposal targets the remaining per-request copy, not the successful cache.
- **Smaller alternative:** expose cached models as read-only request data and clone only a task
  at the action boundary before mutation/save. A less invasive first slice is separate GET and
  POST snapshot methods, retaining today's full isolated snapshot for actions until each is
  proven copy-on-write safe. Do not weaken the stable-within-request guarantee.
- **Benefit / risk / dependencies:** medium-to-high web latency and allocation benefit at large
  task counts; medium concurrency risk because model objects are mutable. No new dependency is
  needed. Verify concurrent GET/action behavior, external file edits, duplicate-ID quarantine,
  phase metadata changes, and config reload gating using the existing benchmark and web tests.

### 3. Make the web benchmark fixture honor its advertised size controls

- **Purpose and callers:** `scripts/benchmark_web_pages.py` is the documented reproducible web
  performance harness and directly exercises the cache comparison. Its CLI exposes `--tasks` and
  `--runs` (`scripts/benchmark_web_pages.py:141-152`).
- **Unnecessary complexity / defect:** `ROUTES` always includes task `BM-0001` and run
  `seed-0001` (`scripts/benchmark_web_pages.py:23`), while the fixture accepts zero or one task and
  zero runs. Running `--tasks 600 --runs 0 --events 0 --repeats 5` failed with HTTP 404 on the
  hard-coded run route. That makes small, fast experiments require unrelated fixture data and
  obscures what a parameter controls.
- **Smaller alternative:** validate positive minima with an explicit parser error, or derive
  routes from generated fixture identities and omit the run page when runs are zero. The latter
  keeps zero-cost discovery experiments useful.
- **Benefit / risk / dependencies:** small maintenance and experiment-reliability benefit; low
  risk and no dependencies. Verify zero/one/default sizes and keep output schema stable.

### 4. Share the duplicated optional-file reader

- **Purpose and callers:** `garden.kickoff._read` and `garden.retro._read` independently implement
  the same five-line optional-path read with identical `OSError` fallback
  (`src/garden/kickoff.py:55-59`, `src/garden/retro.py:134-138`). They are internal helpers used
  while assembling model briefs.
- **Why simplify:** this is confirmed duplicate implementation, not a hot path. A single private
  helper near the other context-document reading logic removes one error-policy copy. Do not make
  it public API or add a new abstraction layer solely for this pair.
- **Benefit / risk / dependencies:** very small readability benefit; low risk. It is best folded
  into the next change touching both brief builders, not scheduled alone unless cleanup batching
  is desired. Preserve missing-file and read-error behavior in focused kickoff/retro tests.

### 5. Consolidate task-specific capture scripts only after preserving reproducibility

- **Purpose and callers:** `capture_cg410.py`, `replay_cg381_inbox.py`, and
  `replay_cg455_now.py` are one-task served-interaction harnesses. Their only repository consumers
  are their own receipts/historical evidence, while `walkthrough.py` and
  `benchmark_web_pages.py` provide maintained general harnesses.
- **Why it may be removable:** root-level scripts enlarge the apparent supported tooling surface
  and repeat disposable garden/server/capture setup. This is a maintenance observation, not a
  runtime claim.
- **Bounded alternative:** first extract only demonstrably common fixture/server setup into an
  existing general harness; then either move historical scripts beside their evidence or replace
  them with declarative inputs that reproduce the same checks. Do not simply delete them: current
  evidence receipts name the commands, so removal without a reproducible replacement would break
  an existing documented consumer.
- **Benefit / risk / dependencies:** low-to-medium maintenance benefit; medium archival risk. No
  new dependency. Treat each evidence family independently and keep recorded head/environment
  distinctions intact.

## Candidates retained

- **Store config reload split:** keep `config_changed_on_disk`, `load_config_from_disk`, and
  `adopt_config`. The scheduler fence deliberately inspects a pending executable configuration
  before adoption (`src/garden/scheduler/fence.py:882-900`); collapsing it into
  `reload_config_if_changed` would reopen a security race.
- **Duplicate-ID quarantine and ID reservations:** keep both. Duplicate IDs are excluded from
  dispatch and surfaced by validate/doctor/tick, while reservations prevent concurrent retro and
  live task creation from allocating the same ID. These are concurrency and recovery safeguards,
  not redundant caches (`src/garden/store.py:229-254, 443-535`).
- **`Store.control_task`:** keep the bounded canonical lookup used by recovery launch
  (`src/garden/web/actions/tasks.py:410-443`). It avoids making an incident control depend on a
  potentially expensive or broken full garden scan.
- **Dynamic CLI/plugin/runner registration:** keep CLI family imports, configured Python check
  callables, and runner adapter imports. Their use is established by decorators/configuration,
  so absence of ordinary direct calls is expected (`src/garden/cli/__init__.py:1-17`,
  `src/garden/checks.py:118-129`, `src/garden/runner/__init__.py:38-57`).
- **Dispatch and production enrollment recovery journals:** `_dispatch` and `_ensure_locked` are
  difficult to read, but manual inspection found ordering contracts for process ownership,
  branch leases/backups, exact brief criteria, scoped credentials, resumable partial creation,
  publication, and revocation. A broad rewrite would put security, idempotency, and interruption
  recovery at risk. Future changes should extract independently testable value construction only;
  the journal transitions and locks should remain explicit.
- **Automerge gates:** `_automerge_gate` is branch-heavy, but each branch represents a supported
  safety/policy reason displayed to operators: phase holds, exact reviewed head, CI provider,
  human review, budgets, protected paths, queue ownership, and hard-tier scratch merge. Splitting
  the function for readability is reasonable only if the ordered “first reason” contract and all
  gates remain visible together; no removal is justified by this audit.

## Proposed implementation follow-ups

1. **Incremental, identity-safe GitHub feedback polling** — preserve current trusted-author,
   notice, equal-timestamp, restart, and failure behavior; demonstrate fewer provider page/process
   reads for unchanged PR history.
2. **Copy-on-write web discovery snapshots** — preserve per-request consistency and concurrent
   action safety; compare the same 120- and 600-task fixtures before/after under the same resource
   limits.
3. **Parameter-safe web benchmark fixtures** — define zero/one-size behavior, keep the JSON output
   contract, and add a small script test.
4. **Consolidate historical capture harness setup** — only after mapping each receipt to an
   equivalent reproducible command; retain evidence provenance.

The first three are independent and intentionally bounded. The optional-file helper is too small
to justify its own task and should ride with related brief-builder maintenance.
