# CG-624 accepted-source baseline

This record freezes the pre-edit source and collection for the requested 40% test-suite
reduction. No test or product file had been changed when these commands ran.

## Source and collection

- Source: `41ff4de3d273afb2c4ad642856e2ff893ee7db31` (accepted `main` head)
- Interpreter: CPython 3.14.4
- Full expanded collection: **2,888 cases**
- Ordinary selection: **2,880 selected, 8 deselected**
- Opt-in inventory: **5 stress cases** and **3 browser cases**
- Count target at this baseline: at most **1,728 ordinary cases**

The counts were obtained with `PYTEST_ADDOPTS` removed because the supervised worker
environment supplied three temporary node deselections. This makes the inventory match the
repository command rather than allowing an ambient selection override to lower the baseline:

```sh
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest --collect-only -q
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest --run-stress -m stress --collect-only -q
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest --run-browser-tests -m browser --collect-only -q
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest --run-stress --run-browser-tests --collect-only -q
```

The ordinary CI pytest command is `.venv/bin/python -m pytest -q`; repository configuration
sets a 120-second test deadline and a 900-second session deadline. CI and ordinary collection
exclude stress and browser markers in `tests/conftest.py`.

## Environment

The attempted local measurement used WSL2 Linux 6.6.87.2, two logical CPUs, 7.7 GiB RAM,
2 GiB swap, and the prepared project environment. Key versions were pytest 9.1.1,
pytest-timeout 2.4.0, FastAPI 0.141.1, Starlette 1.6.0, httpx 0.28.1, PyYAML 6.0.3, and
ruff 0.16.7. Dependencies were already installed. Pytest caches and the disposable
`--basetemp` were empty for the cold attempt; dependency and admission time were excluded.

## Hosted runtime baseline

GitHub Actions [run 34601219310](https://github.com/joshmarcus/context-garden/actions/runs/34601219310)
measured the exact accepted source twice in one Ubuntu 24.04 hosted job. Workflow source
`340f01f4a7f1be85f6948db5b963d61036a42089` verified the application source was
`41ff4de3d273afb2c4ad642856e2ff893ee7db31` before testing. Both serial runs used CPython
3.12.14, pinned prepared dependencies, four logical CPUs (affinity 0-3), 120-second
per-test and 900-second session limits, and no ambient `PYTEST_ADDOPTS`. Each passed 2,876,
skipped 4, and deselected 8. Pytest execution wall time was **611.673106438 seconds cold**
and **623.780750659 seconds warm**. The cold run cleared pytest caches and test-created
temporary state; the warm run immediately followed with dependencies and runner unchanged.
Queue/admission and dependency installation were outside both measurements.

The candidate targets are therefore at most 1,728 ordinary cases, 367.003863863 seconds
cold, and 374.268450395 seconds warm.

## Earlier runtime attempt and limitation

GitHub Actions subsequently completed the repository's ordinary CI command at the exact
accepted source in [run 34592989538](https://github.com/joshmarcus/context-garden/actions/runs/34592989538).
The Ubuntu hosted job used CPython 3.12.14 and passed **2,876 tests**, skipped **4**, and
deselected the same **8** opt-in cases in **602.68 seconds**. Its slowest-test table and
timestamps are retained in the Actions log. The job began from a fresh checkout and installed
the development dependencies before pytest; installation and queue time are separate workflow
steps and are not included in pytest's reported wall time.

This successful run proves the exact accepted suite completes in the authorized hosted
environment and supplies one cold-like observation. It is not represented as the contract's
serial cold/warm denominator because the workflow ran pytest only once and did not use the
specified paired cache conditions. A second ordinary run on a different hosted runner would
also not be a warm repetition with unchanged runner state.

The exact command was:

```sh
timeout --signal=TERM --kill-after=10s 900 "$GARDEN_VALIDATION_RUNNER" \
  -m garden.validation -- .venv/bin/python -m pytest -q --durations=40 \
  --timeout=120 --timeout-method=thread --cache-clear --basetemp="$measurement_root/cold"
```

The unchanged accepted source did not produce a complete baseline. It reported a failure at
about 10% and reached only 20% before the validation supervisor terminated the process while
`tests/test_canary.py::test_scenarios_pass_on_a_good_build` was polling in
`garden.canary._drive`. There was no pytest summary or duration table, so this attempt is not
represented as a wall-time baseline and a warm run was not started.

The closest complete historical result remains the CG-453 AWS profile at source
`3c037ca4804a9015c161bd6ecf505474bb71f2d5`: 1,713 passed, 3 skipped, and 4 stress cases
deselected in 424.93 seconds cold and 432.31 seconds warm on Linux/Python 3.12.14. It is useful
context but is neither the current source nor a valid denominator for CG-624's required
before/after comparison.

## Candidate consolidation

The candidate replaces five Cartesian products with representative pairings. These axes
reach the same assertion path independently, so their cross-product repeated behavior rather
than protecting an interaction. Every axis value remains directly collected:

| Removed combinations | Protected behavior and retained coverage |
| ---: | --- |
| 8 | Every canonical consumer claims its checkout before Git operations; all four consumers and all three unsafe states remain represented. |
| 6 | Recovery references do not match shorter run IDs; both modern and legacy punctuation plus all six reference encodings remain represented. |
| 3 | Shell read operands are not mistaken for writes; both transcript harnesses and all three redirect/copy forms remain represented. |
| 4 | Runtime metadata and lease files reject foreign and non-regular `fstat` results; every file class and both non-regular modes remain represented. |
| 4 | Pre-created hostile runtime files are rejected; every lease class and both symlink and FIFO attacks remain represented. |

At the candidate source, ordinary collection is **2,855 selected** and the full expanded
inventory is **2,863**, with the same 8 opt-in cases. This is a 25-case (**0.87%**) ordinary
reduction, leaving a gap of 1,127 cases to the 1,728 target. The 21 retained representative
cases pass in 2.35 seconds on the prepared Linux development worker. No comparable hosted
candidate cold/warm measurement has run, so the runtime reduction and remaining runtime gap
are not yet established. macOS, native Windows, and Windows-through-WSL were not tested.
