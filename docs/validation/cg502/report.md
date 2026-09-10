# CG-502 validation-cost audit

This audit identifies reductions that preserve the behavior contracts of the ordinary test
suite. It does not remove or deselect tests. Stress separation and validation-runner
recovery are already covered by CG-426, CG-441, CG-468, and CG-469 and are not proposed
again here.

## Evidence and limits

The most recent complete profile is the [CG-453 serial ordinary-suite profile](../cg453/report.md).
At source `3c037ca4804a9015c161bd6ecf505474bb71f2d5`, a prepared AWS Linux host ran 1,713
tests (3 skipped, 4 stress tests deselected) in **424.93 seconds cold** and **432.31
seconds warm**. The supervised wrapper took 427.90 and 435.26 seconds. Pytest peak RSS was
278,664 and 278,264 KiB. The two repetitions agree within 1.7%, so the roughly seven-minute
cost and the leading upgrade/canary nodes are repeatable rather than one slow outlier. The
host was Linux 7.0.0-1012-aws x86_64, Python 3.12.14, CPUQuota 300%, MemoryMax 12 GiB, no
swap, and serial pytest. Dependencies and Chromium were already installed; their setup cost
is visible as separate CI steps but no trustworthy duration receipt was retained.

A bounded current-head sample at `582c6e716bc84a7760f41ac0f1557de1a16b568a` ran twice
through `garden.validation`, using a fresh configurable `--basetemp` for each repetition:

```sh
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" \
  -m garden.validation -- /usr/bin/time -f 'elapsed=%e peak_rss_kib=%M' \
  .venv/bin/python -m pytest --timeout=120 --timeout-method=thread \
  --durations=0 --basetemp="$measurement_root/rep-N" -q <nine selected nodes>
```

The measured environment was WSL2 Linux 6.6.87.2, CPython 3.14 from the prepared project
venv, 2 logical CPUs, 7.7 GiB RAM (5.7 GiB initially available), and 2 GiB swap. The two
nine-test selections passed in 53.35 and 54.68 seconds (process wall 54.22/55.20 seconds),
with peak RSS 140,124/155,788 KiB:

| Test or parametrized case | Repetition 1 | Repetition 2 | What dominates |
| --- | ---: | ---: | --- |
| `test_upgrade.py::test_real_serve_auto_upgrade_reexecs_and_serves_new_build` | 22.89s | 24.12s | copy, Git, venv, pip, server, exec |
| `test_canary.py::test_self_check_passes_on_the_current_build` | 7.47s | 7.59s | QA plus both canary scenarios |
| `test_canary.py::test_exits_non_zero_when_a_scenario_fails` | 6.49s | 6.58s | repeats QA and the unaffected scenario |
| `test_remote_worker.py::test_worker_renews_short_lease_during_setup_and_check` | 4.72s | 4.78s | two intentional 2s sleeps |
| `test_qa.py::test_scripted_agent_completes_every_flow` | 4.52s | 4.52s | served nine-flow journey |
| remote lifecycle, standalone | 2.52s | 2.63s | server, CLI child, Git/check lifecycle |
| remote lifecycle, managed | 2.45s | 2.55s | nearly the same journey plus host lock |
| retro abandoned-reservation rerun | 1.01s | 1.07s | two full retro/Git journeys |
| retro reservation collision | 0.55s | 0.56s | full retro/Git journey |

These focused numbers are comparative, not a replacement current-head full-suite result.
The AWS and WSL Python versions and resource limits differ. macOS and native Windows were
not measured; macOS full-suite scheduling also differs, while WSL exercises the portable
Linux paths used by the commands. All experiments used disposable pytest/temp fixtures and
did not touch a live garden or worker.

## Recurring-cost assumptions

The workflow runs the ordinary suite on both pushes to `garden/**` and `pull_request`
events. For an in-repository PR update that produces both events at the same SHA, that is
two equivalent GitHub test jobs. Owner policy also calls for one final AWS ordinary-suite
validation. Thus a representative final revision currently pays about **3 × 428.62s =
21.4 runner-minutes** of pytest (midpoint of the AWS cold/warm pair), excluding install and
queue time. A revision before a PR exists may have only the push job; fork PRs do not match
the `garden/**` push trigger. Exact event counts were not available in the supplied run
context, so this is an explicit per-revision model, not a claimed historical total.

The scheduled QA workflow runs `garden qa --scripted` once daily in addition to ordinary
suite coverage. That run is intentional monitoring, not a third copy within pytest, and is
not counted in the per-revision estimate.

## Ranked recommendations

| Rank | Action | Estimated saving | Confidence | Correctness risk |
| ---: | --- | ---: | --- | --- |
| 1 | **Consolidate equivalent same-SHA GitHub jobs** while retaining an exact-head required check for pushes, same-repository PRs, and forks | about 428.62s per duplicate event; up to 7.1 runner-min/revision | medium | medium |
| 2 | **Narrow canary composition/failure tests**; retain one real QA journey and both real canary scenarios | 13.96–14.17s/full suite; about 42s under the three-run model | high | low |
| 3 | **Narrow the two remote lifecycle variants** to one shared TCP/CLI journey plus a focused managed-lock boundary | about 2.5s/full suite; about 7.5s/revision | medium | medium |
| 4 | **Narrow retro reservation orchestration** to one integration path and use existing Store tests for release/prune details | roughly 1.0s/full suite; about 3s/revision | medium | low-medium |
| 5 | **Retain** the real upgrade/re-exec test | no saving | high | high if removed |
| 6 | **Retain** the short-lease renewal test and scripted QA journey | no saving | high | high if timing/transport is mocked away |

### 1. Equivalent workflow invocations

`.github/workflows/ci.yml` requests the same lint and full pytest job for a branch push and
the corresponding PR event. These protect exact-head merge eligibility and fork PRs, but
for a same-repository branch they assert the same source with the same Ubuntu/Python setup;
the second run has very little marginal regression value. Preserve one required exact-head
result and the ability for `scripts/check_ci.py` to find its push-triggered run. A follow-up
should first confirm branch-protection/check-name behavior, then condition or split the PR
job so forks remain covered without rerunning same-repository heads. Do not replace this
with a stale-SHA or latest-main result.

### 2. Canary composition and failure reporting

`tests/test_canary.py::test_self_check_passes_on_the_current_build` reruns
`tests/test_qa.py::test_scripted_agent_completes_every_flow` and the same two scenarios
covered by `test_scenarios_pass_on_a_good_build`. The failure test monkeypatches only the
stacked scenario but still runs QA and the unaffected merge-queue scenario. Its valuable
contract is that one failed row makes the CLI nonzero and names the failure, not that every
other integration succeeds again.

Replace the two composition tests with cheap orchestration tests that inject QA/scenario
rows, while retaining the real QA integration and `test_scenarios_pass_on_a_good_build`.
The latter protects the actual CG-173 child-retarget and CG-176 pending-rollup failures;
focused scheduler tests remain the cheaper diagnostic layer beneath it.

A disposable comparison injected an orphaned-child failure `CanaryReport` at the
`self_check` boundary and invoked the real CLI. It took **0.20s and 0.19s**, peak RSS
42,200/42,208 KiB, and on both repetitions asserted exit 1, `canary: FAILED`, and the
specific `child orphaned` detail. The existing end-to-end failure test took 6.49/6.58s.
This demonstrates equivalent failure propagation and a **96.9–97.1%** reduction for that
test. The known regression still reaches the replacement through a failed scenario row,
while the retained real scenario test continues to fail if retargeting itself breaks.

### 3. Remote lifecycle variants

The standalone and managed parameters in
`tests/test_remote_worker.py::test_remote_lifecycle_over_served_http` both start FastAPI,
launch a separate CLI process, exercise authentication, lease rejection, setup, check,
result transport, and PR creation. Managed mode adds the material lock/setup-count
contract, but repeating every transport assertion contributes little. Keep one complete
standalone TCP/CLI journey. Cover the managed host lock and setup cache with a smaller
managed-boundary test using the same worker execution entry point; do not reduce it to
source-text assertions. This is lower priority because a refactor could accidentally make
the modes diverge, so the follow-up should compare the assertion inventories first.

### 4. Retro reservation journeys

The two retro tests build repositories, worktrees, commits, and PR state to reach reservation
bookkeeping. `tests/test_store.py` already directly proves allocation skips, owner-batch
release, restart persistence, and pruning when task files appear. Retain the collision test
as the scheduler/worktree integration contract. Narrow the abandoned-rerun case to prove
that `start_retro` releases and retakes the owner batch without executing a second complete
retro. Avoid implementation-mirroring assertions: the preserved outcome is stable ID reuse
without collision, not which private helper is called.

### 5. Expensive tests worth their cost

The 23–24 second upgrade test uniquely crosses a real installed-package, pip, server socket,
`os.execv`, PID continuity, active-build identity, and lifecycle-event boundary. The faster
upgrade tests mock installer/restart behavior and cannot preserve that protection. Retain it
in the ordinary suite; moving it optional would expose the production upgrade boundary only
during an actual pin change.

The short-lease test spends four seconds deliberately proving heartbeats prevent reclaim
during both setup and pre-PR check execution. A fake clock would not exercise the renewal
thread against real elapsed leases. Retain it unless the production API gains a deterministic
synchronization hook; merely shortening its sleeps risks turning it flaky. Likewise, retain
the 4.52-second scripted QA journey: it is the sole compact nine-flow served application
contract, while the daily workflow detects environmental drift on a different cadence.

## Follow-up boundaries

The worthwhile changes are four independently reviewable tasks: deduplicate same-SHA CI
events, consolidate canary composition tests, narrow the remote lifecycle variants, and
narrow retro reservation orchestration. Their task drafts are emitted in this run's
`discovered` result so scheduler-owned task files remain untouched. None duplicates
CG-426's opt-in stress selection, CG-441's bounded regression, or CG-468/CG-469's nested
validation recovery.
