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

## Runtime attempt and limitation

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
