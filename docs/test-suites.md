# Focused test suites

Run pytest serially while iterating. A targeted pass proves only the named
responsibility; `python3 scripts/check_ci.py` remains the full ordinary-suite regression gate
for the final committed branch. Stress/load experiments are excluded from both ordinary
pytest commands and routine CI.

Ordinary pytest tests have a 120-second per-test deadline from pinned `pytest-timeout`.
The cooperative 900-second pytest session deadline improves diagnostics between tests.
Detached local and remote check batches, plus validation commands issued through
`garden.validation`, independently enforce the configured budget with a hard 900-second
ceiling. Admission waiting is recorded separately and does not consume that clock. A hard
timeout writes `validation_timeout.json`, exits nonzero, terminates only the validation's
owned descendants, and releases the shared validation slot without stopping the parent model
session.

```bash
.venv/bin/python -m pytest tests/test_retro_documents.py -q
.venv/bin/python -m pytest tests/test_retro.py -q
.venv/bin/python -m pytest tests/test_fixture_git.py -q
.venv/bin/python -m pytest tests/scheduler -q
```

The commands above cover, respectively, pure retro document rendering/parsing, retro
orchestration with its temporary Git topology, fixture Git cleanup, and scheduler lifecycle.


## Stress and load experiments are opt-in

`pytest -q` and focused test-file commands exclude tests marked `stress` before their
fixtures run. The default CI workflow uses this same policy. Explicitly naming a stress
node or using `-m stress` does not opt in by itself.

Stress tests include large retained-history latency measurements, generated CPU/memory
workloads, and concurrent served overload journeys. Keep deterministic functional tests,
including small concurrency/locking regressions, in the ordinary suite. Mark future tests
that deliberately generate sustained load, pressure, large histories, or benchmark latency
with `@pytest.mark.stress`.

Run a deliberate, separately bounded experiment only in a disposable environment:

```bash
# Inspect the opt-in selection without running any workload.
.venv/bin/python -m pytest --run-stress -m stress --collect-only -q
# Explicitly authorized stress experiment; normal worker/CI validation never needs this.
.venv/bin/python -m pytest --run-stress -m stress tests/test_web.py -q
```

Use the task's CPU, memory, process and time limits for the experiment. A worker addressing
a functional failure should select the relevant ordinary tests; do not opt in to stress
as a substitute for obtaining CI failure details. A stress failure belongs to this separate
experiment and must not force unrelated implementation revisions.

## Select the smallest relevant suite

| Changed responsibility | Start with | Also run when the boundary changes |
| --- | --- | --- |
| Offline model, store, graph, brief, criteria, or parsing code | Its matching `tests/test_<area>.py` | `tests/test_cli.py` if a command exposes it; `tests/scheduler` if scheduler decisions consume it |
| Scheduler phase (`dispatch`, `reap`, `poll`, `human`, quotas, state) | `tests/scheduler/test_<phase>.py` | The directly adjacent scheduler phase tests and affected CLI/web action tests |
| CLI command or output | `tests/test_cli.py` | The underlying model/store/scheduler suite |
| Web page or action | Its `tests/test_web.py` or `tests/test_now*.py` case | The action's scheduler/CLI suite; actual-app interaction checks still apply to UI work |
| Runner, harness, Git/worktree, onboarding, or retro lifecycle | The matching `tests/test_runners.py`, `test_harness.py`, `test_gitops.py`, `test_onboard.py`, or `test_retro.py` | `tests/test_fixture_git.py` when Git fixture setup changes |
| Retro renderers, parsing, or document layout | `tests/test_retro_documents.py` | `tests/test_retro.py` if the generated document crosses the scheduler/worktree boundary |

`tests/conftest.py` is deliberately lightweight at import time: its two autouse fixtures
only patch the runner registry and environment. The `garden` fixture is lazy and creates a
repository, bare remote, push, and clone only for tests that request it. Do not add network,
Git, browser, or worker startup to an autouse fixture. The fixture Git helper has a
30-second process-group timeout so a failed command cannot leave a descendant holding
captured output open; failed fixture directories remain available to pytest diagnostics.

For changes to shared fixtures, common configuration, package wiring, or an unclear impact,
run the named focused suites plus `tests/scheduler`, `tests/test_cli.py`, and the relevant
web/integration suite. If the impact is still unclear, use the conservative fallback:
commit and run the final gate below rather than treating a narrow pass as full coverage.

```bash
python3 scripts/check_ci.py
```

## Serial timing comparison

## Ordinary-suite runtime diagnostics

The ordinary suite is serial.  Its runtime target applies to pytest after dependencies and
the Chromium browser are prepared: installation, validation admission wait, and an
unfinished descendant are recorded separately and never counted as a passing test run.
Run the command below from the exact commit being measured, with no other validation using
the host slot.  It preserves the 120-second test and 900-second session caps, clears pytest's
cache, and gives tests an empty temporary-state root for a cold measurement.

```bash
runtime_root=$(mktemp -d)
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  .venv/bin/python -m pytest -q --durations=40 --cache-clear \
  --basetemp="$runtime_root/cold"
```

For the warm measurement, immediately repeat at the same source and limits with a distinct
`--basetemp="$runtime_root/warm"` and without `--cache-clear`.  Keep the terminal output:
pytest reports the collected/deselected totals, wall time, and current slowest nodes; the
validation run's `execution.json` distinguishes `running`, `waiting` (admission), and a
completed process with descendants still alive.  CI runs the same ordinary selection with
`--durations=40`, so a regression leaves the slow-node table in its job log.  Stress tests
remain excluded unless `--run-stress` is explicitly supplied.

The current representative AWS measurement and environment are recorded in the
[CG-453 report](validation/cg453/report.md): 1,713 passed, 3 skipped and 4 stress tests
deselected in 424.93s cold and 432.31s warm on Python 3.12.14. Compare the slowest-node
table before changing fixtures; a validation record still in `waiting` is admission time,
while a completed pytest process whose wrapper remains live indicates a descendant leak.

For a retro document renderer/parser change, the baseline must precede the extraction.
This comparison uses `58e13b99ddaf62b751b01771e5660039a421d46c`, the parent of split commit
`4cfcc8c`, with its original **34-test** mixed `tests/test_retro.py`. The focused selection
uses `tests/test_retro_documents.py` at `1f3cbb29270119fc84630e8f8abf00ce4077409b`.
The remaining 28 lifecycle tests are retained; this measures iteration selection, not a
reduction in full-suite coverage.

Both selections ran serially on 2026-09-07 in one systemd user service with CPU quota200%,
MemoryHigh512MiB, MemoryMax1GiB, swap disabled and TasksMax128. The benchmark verified
`cpu.max=200000 100000` and `memory.max=1073741824` from its own cgroup. Both used the same
installed Python interpreter, checkout-specific `PYTHONPATH`, disabled bytecode writes,
disk-backed temporary directories and fresh equivalent `--basetemp` paths.

`/usr/bin/time` measured process wall time and peak RSS. Temp use is **only** `du -sk` of
each pytest `--basetemp`, excluding the baseline archive/checkout, logs and auxiliary temp.
The pure document suite never created that directory, so its pytest-temp allocation is0.

| Selection | Suite | Tests | Elapsed | Peak RSS | Pytest temp |
| --- | --- | ---: | ---: | ---: | ---: |
| Before extraction | Historical `tests/test_retro.py` | 34 passed | 6.29s | 67,084KiB | 14,112KiB |
| Focused document iteration | `tests/test_retro_documents.py` | 6 passed | 0.27s | 54,060KiB | 0KiB |

The complete benchmark service peaked at153.8MiB, with no swap. This is one ordered pair
on a shared host, not a full-suite speedup claim or a general performance estimate. The
six-test document selection avoids Git topology; lifecycle checks remain necessary when
those responsibilities change, and full GitHub CI retains both suites.

The [report](validation/cg354/report.json), test/time logs and original measurement script
are committed under `docs/validation/cg354/`. Both pytest invocations passed. The original
report collector then failed because it ran `du` on the absent focused-test temp directory;
the report was recovered from the retained logs and directories without rerunning tests.
The reusable collector handles an absent directory as zero allocation.

To repeat the comparison for the current checkout, from the repository root:

```bash
benchmark_root=$(mktemp -d /home/joshua/work/operator-test-tmp/cg354-repeat.XXXXXX)
benchmark_python="$PWD/.venv/bin/python"
systemd-run --user --wait --pipe --collect \
  --property=CPUQuota=200% --property=MemoryHigh=512M \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  --property=TasksMax=128 --property=RuntimeMaxSec=180 \
  "$benchmark_python" "$PWD/docs/validation/cg354/reproduce.py" \
  --repo "$PWD" --output "$benchmark_root/results"
```

Use an absolute interpreter path with the development dependencies installed. The measured
run used `/home/joshua/garden/.venv/bin/python`. `reproduce.py` archives the actual pre-split
revision outside the measured temp paths, runs `/usr/bin/time -f
'elapsed=%e\npeak_rss_kib=%M'` around each serial pytest command, measures precisely
`du -sk <result>/pytest` (or0 if absent), and records revisions, commands, limits, hashes,
results and the measurement scope in `report.json`. It refuses tracked source/test edits
that would make the recorded current revision misleading.
