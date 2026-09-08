# CG-453 serial ordinary-suite profile

Measurements are serial Linux/Python 3.12 runs through `garden.validation`, with the
configured 120-second per-test and 900-second validation limits. Dependency and browser
installation are not included.

## Definitions

- **Cold:** the first pytest invocation at an exact source revision, with `--cache-clear`
  and a fresh `--basetemp` directory.
- **Warm:** an immediate second invocation at unchanged source and limits, with a distinct
  `--basetemp` directory and no cache clear.

## Before changes

Source `93681f4e74bd63996960057219a86c3e83296ed2` started with:

```sh
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  .venv/bin/python -m pytest -q --durations=40 --cache-clear \
  --basetemp=/tmp/cg453-cold-baseline
```

Pytest completed its test process after more than twelve minutes, but two detached remote
check supervisors remained in `waiting` state on the enclosing validation slot. The parent
validation therefore ended unsuccessfully rather than supplying a trustworthy pytest wall
time. This distinguished a descendant leak from admission waiting and led to the isolated
remote-host runtime fixture repair.

## Current cold profile

Source `f95662ad9cfad26d060ffb4502eed08bb5c7709b` ran:

```sh
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  sh -c '.venv/bin/python -m pytest -q --durations=40 --cache-clear \
  --basetemp=/tmp/cg453-cold-f95662a > /tmp/cg453-cold-f95662a.log 2>&1'
```

Result: **1709 passed, 4 skipped, 4 stress tests deselected, 2 failed in 580.66s**.
The failed nodes were `tests/test_onboard.py::test_onboard_this_repository_uses_documented_setup_and_ci_tests`
(the workflow diagnostics change altered its expected discovered command) and
`tests/test_upgrade.py::test_real_serve_auto_upgrade_reexecs_and_serves_new_build`
(the disposable pip invocation rejected its local git requirement). The latter is unrelated
to this change and needs a base-environment comparison before it is repaired.

The 40 slowest calls were led by the upgrade re-exec test (11.42s), current-build canary
(10.66s), walkthrough capture (9.30s), failing-canary scenario (9.14s), retro reservation
recovery (8.37s), remote HTTP lifecycle (7.87s and 7.61s), QA flow (6.41s), and remote
short-lease setup/check (5.98s). These timings retain ordinary application, browser,
worker, Git, and lifecycle coverage; no nodes were skipped or xfailed for the measurement.

The copied, independent Git fixture seed removed repeated initial repository creation and
initial push from each requesting test. Its files are copied rather than linked, so a test
cannot alter the session seed or another test's mutable remote/worktree state.

## Status

The cold target has not yet been met and no warm result is claimed. The profile is retained
to make the remaining work reproducible without confusing dependency installation,
admission waiting, descendant leaks, and pytest execution.
