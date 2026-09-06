# Focused test suites

Run pytest serially while iterating. A targeted pass proves only the named
responsibility; `python3 scripts/check_ci.py` remains the full-suite regression gate
for the final committed branch.

```bash
.venv/bin/python -m pytest tests/test_retro_documents.py -q
.venv/bin/python -m pytest tests/test_retro.py -q
.venv/bin/python -m pytest tests/test_fixture_git.py -q
.venv/bin/python -m pytest tests/scheduler -q
```

The commands above cover, respectively, pure retro document rendering/parsing, retro
orchestration with its temporary Git topology, fixture Git cleanup, and scheduler lifecycle.

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

The representative change is a retro document renderer/parser edit. Both measurements ran
serially on this worker on 2026-09-06, with a fresh `--basetemp` directory and
`/usr/bin/time`; temp use is the final size of that directory. The before command named the
former mixed module, while the after command names only the extracted pure-document suite.

| Selection | Command | Tests | Elapsed | Peak RSS | Temp use |
| --- | --- | ---: | ---: | ---: | ---: |
| Before | `.venv/bin/python -m pytest tests/test_retro.py -q --basetemp=/tmp/cg354-retro-before/pytest` | 34 passed | 3.37 s | 67,768 KiB | 8,152 KiB |
| After | `.venv/bin/python -m pytest tests/test_retro_documents.py -q --basetemp=/tmp/cg354-retro-documents-after/pytest` | 6 passed | 0.31 s | 54,092 KiB | 4 KiB |

The focused document loop is about 11 times faster and avoids the temporary Git topology.
The remaining retro lifecycle suite intentionally still pays for repository/worktree setup:
those tests validate Git-backed behavior and should not be disguised as unit tests. Full
suite duration is not repeated locally; it remains a GitHub CI gate, and its fixture Git
setup now fails promptly rather than waiting indefinitely for an orphaned output holder.
