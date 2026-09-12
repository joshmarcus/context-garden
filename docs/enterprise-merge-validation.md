# Enterprise merge validation

Validated source: `0c6b2c838c007af9c1f5101939e80a9f4e900fff` (accepted `main`,
September 12, 2026).

This head combines the following accepted changes:

- PR 495, merge `d9fafe16`: exact-head command validation.
- PR 496, merge `afc81b67`: authoritative frozen validation plans.
- PR 497, merge `9373a871`: durable, recoverable SSH/tmux execution.
- PR 502, merge `8e0af183`: retirement of terminal-task infrastructure holds.
- PR 501, merge `0c6b2c83`: recovery when an automated review omits a verdict.

## Checks and results

The focused regression selection was run through the bounded validation supervisor:

```text
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" \
  -m garden.validation -- .venv/bin/python -m pytest \
  --timeout=120 --timeout-method=thread \
  tests/test_ci_status.py tests/scheduler/test_poll.py \
  tests/test_criteria.py tests/test_preflight.py tests/test_review.py \
  tests/scheduler/test_human.py tests/test_ssh_sessions.py -q
```

Result: **416 passed** in 116.44 seconds. Two dependency deprecation warnings were
reported by FastAPI/Starlette test helpers; there were no test failures.

The selection verifies that command results are bound to the queried PR head and an
old green result cannot clear the merge gate after the head moves. It also verifies
that dispatched criteria and validation plans remain frozen, and that a later task
edit or stale check cannot replace the current-head plan. Review output without a
verdict enters bounded review recovery, retains its logical round and head, and does
not become approval. Terminal-task cleanup removes stale infrastructure and recovery
state while preserving active check ownership and explicit owner decisions.

The SSH session tests verify transport-loss recovery against the same run and tmux
session, exact checkout leases, bounded uncertainty, cancellation of only owned
descendants, terminal-receipt recovery after a lost acknowledgement, failed-run
publication, and preservation of the fatal diagnostic rather than an incidental
transport message. Large output is retained without loss or duplication, and stale
acknowledgements cannot release another run's checkout.

Actual tmux was also exercised outside the fake-tmux test fixture in a disposable local
Git repository, using tmux 3.4 and the shipped `garden.ssh_session` protocol. A harmless
detached session emitted `probe-start` and `probe-complete`, reached one terminal receipt
with exit code 0, reported the repository's exact HEAD, branch and clean status, and was
then acknowledged. This is a local transport/session probe, not a claim of a live remote
SSH journey or model execution.

The existing hosted workflow for this exact combined source, run
`34700272684`, was reused rather than repeated. Its quality job, three test shards and
aggregate succeeded: 3,000 tests passed, 4 skipped and 8 deselected; the push-only
exact-head job was correctly skipped for that push event. This source verification does
not assert that the independently published production controller or worker daemons have
been upgraded from stable 0.3.0.
