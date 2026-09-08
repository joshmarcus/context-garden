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

## Final comparison

The remaining profile showed that walkthrough HTML-to-text conversion reparsed every CSS
selector for every element. The final implementation caches at most 512 selector component
lists and 1,024 simple selectors, keyed by the complete selector text, and builds a fresh
id/class/tag rule index for each parsed page. Unsupported selectors remain conservative
(visible), and no page-specific declarations or mutable capture state enter either cache.

On source `3c037ca4804a9015c161bd6ecf505474bb71f2d5`, the cold command above (with
`--basetemp=/tmp/cg453-final.mkgbTe/cold-pytest`) passed **1,713 tests**, skipped 3, and
deselected the same 4 explicitly marked stress tests in **424.93s**; the supervised wrapper
took 427.90s and pytest's peak RSS was 278,664KiB. The immediate warm command removed
`--cache-clear`, used `/tmp/cg453-final.mkgbTe/warm-pytest`, and passed the identical
collection in **432.31s**; its wrapper took 435.26s and peak RSS was 278,264KiB. Both were
serial and passed below 480 seconds. The slowest final nodes were the real upgrade re-exec
(30.54s cold / 30.95s warm), current-build canary (10.14s / 10.28s), failing canary
(8.88s / 8.95s), and standalone remote lifecycle (8.56s / 8.52s).

The affected `tests/test_walkthrough.py` suite went from **63.38s** in the pre-optimization
cold profile to **19.62s** (36 passed). The combined retro and retro-verdict suites, whose
captures exercised the same conversion path, passed 50 tests in **34.00s**, versus 103.25s
for those files in the cold profile. The Git fixture seed remains copied into independent
mutable repositories; the selector caches are bounded pure parse results and each page's
rule index is discarded with its parser.

The representative host was Linux 7.0.0-1012-aws x86_64, Python 3.12.14 and pip 25.0.1.
Its worker service caps were CPUQuota 300%, MemoryMax 12GiB, no swap and TasksMax 17,120;
pytest itself remained serial. The runner had already prepared dependencies and Chromium,
so installation time was excluded as required but no trustworthy install-duration receipt
was available for this continuation. CI retains separate install steps, making their time
visible independently from the pytest `--durations=40` diagnostic.
