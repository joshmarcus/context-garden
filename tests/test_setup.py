"""Per-product environment setup: `products.<name>.setup` prepares a fresh worktree
(command + env), names the test/lint commands, and nothing assumes Python/pip/uv/a venv."""

from __future__ import annotations

import shlex
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from garden.brief import build_brief
from garden.model import Status
from garden.proctree import pid_alive
from garden.runner.base import RunnerError, run_setup, setup_marker, setup_stamp
from garden.scheduler import Scheduler
from garden.store import Store

# ---- run_setup unit tests ---------------------------------------------------

def test_run_setup_runs_once_then_reuses(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    tally = tmp_path / "count.txt"
    setup = {"command": f"echo x >> {tally}"}
    run_setup(wt, setup)
    run_setup(wt, setup)  # marker matches → no-op
    assert tally.read_text() == "x\n"
    assert setup_marker(wt).exists()
    # marker lives beside the worktree, never inside the checkout
    assert setup_marker(wt).parent == wt.parent


def test_default_setup_stamp_remains_compatible_with_existing_markers():
    import hashlib

    command = "python -m venv .venv"
    assert setup_stamp(command) == hashlib.sha256(command.encode()).hexdigest()


def test_concurrent_setup_recovery_executes_command_once(tmp_path):
    """A replacement preparation waits for an orphaned setup shell and consumes its
    durable stamp, rather than starting the same setup work again."""
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    tally = tmp_path / "count.txt"
    setup = {"command": f"sleep .2; echo x >> {tally}"}
    threads = [threading.Thread(target=run_setup, args=(wt, setup)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert tally.read_text() == "x\n"


def test_setup_rechecks_storage_after_waiting_for_lock(tmp_path, monkeypatch):
    from garden.storage import StorageAdmissionError

    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    launched = False
    checks = 0
    events: list[str] = []

    def storage_check(*args, **kwargs):
        nonlocal checks
        checks += 1
        events.append("storage check")
        if checks == 2:
            raise StorageAdmissionError("local filesystem has 1 free byte")

    def acquire_lock(*args, **kwargs):
        events.append("setup lock acquired")

    def launch(*args, **kwargs):
        nonlocal launched
        launched = True

    monkeypatch.setattr("garden.storage.require_storage", storage_check)
    monkeypatch.setattr("garden.runner.base.fcntl.flock", acquire_lock)
    monkeypatch.setattr("garden.runner.base.subprocess.Popen", launch)

    with pytest.raises(RunnerError, match="local filesystem has 1 free byte"):
        run_setup(wt, {"command": "echo should-not-run"})

    assert checks == 2
    assert events == ["storage check", "setup lock acquired", "storage check"]
    assert not launched
    assert not setup_marker(wt).exists()


def test_run_setup_reruns_when_worktree_is_recreated_at_same_path(tmp_path):
    """A sibling marker from a removed base-probe checkout cannot prepare its replacement."""
    wt = tmp_path / "worktrees" / "T-1.base-probe"
    wt.mkdir(parents=True)
    tally = tmp_path / "count.txt"
    setup = {"command": f"echo x >> {tally}"}
    run_setup(wt, setup, cache_key="probe-generation-1")
    run_setup(wt, setup, cache_key="probe-generation-1")
    wt.rmdir()
    wt.mkdir()

    run_setup(wt, setup, cache_key="probe-generation-2")

    assert tally.read_text() == "x\nx\n"


def test_setup_cache_is_isolated_between_concurrent_task_names(tmp_path):
    worktrees = [tmp_path / "worktrees" / name for name in ("TASK-1", "TASK-10")]
    for worktree in worktrees:
        worktree.mkdir(parents=True)
    tallies = [tmp_path / f"{worktree.name}.txt" for worktree in worktrees]

    threads = [
        threading.Thread(
            target=run_setup,
            args=(worktree, {"command": f"echo {worktree.name} >> {tally}"}),
        )
        for worktree, tally in zip(worktrees, tallies, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert [tally.read_text() for tally in tallies] == ["TASK-1\n", "TASK-10\n"]
    assert setup_marker(worktrees[0]) != setup_marker(worktrees[1])


def test_setup_that_changes_worktree_directory_still_caches(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    tally = tmp_path / "count.txt"
    setup = {"command": f"mkdir -p .venv; echo x >> {tally}"}

    run_setup(wt, setup)
    run_setup(wt, setup)

    assert tally.read_text() == "x\n"


def test_run_setup_reruns_when_command_changes(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    tally = tmp_path / "count.txt"
    run_setup(wt, {"command": f"echo a >> {tally}"})
    run_setup(wt, {"command": f"echo b >> {tally}"})  # different command → runs again
    assert tally.read_text() == "a\nb\n"


def test_run_setup_passes_env(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    out = tmp_path / "env.txt"
    run_setup(wt, {"command": f'printf "%s" "$WIDGET_HOME" > {out}', "env": {"WIDGET_HOME": "/opt/widget"}})
    assert out.read_text() == "/opt/widget"


def test_run_setup_empty_command_is_noop(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    run_setup(wt, {"command": ""})
    run_setup(wt, {})
    run_setup(wt, None)
    assert not setup_marker(wt).exists()


def test_run_setup_failure_raises_with_log(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    log = tmp_path / "setup.log"
    with pytest.raises(RunnerError, match="setup command failed"):
        run_setup(wt, {"command": "echo boom-details >&2; exit 7"}, log_path=log)
    assert "boom-details" in log.read_text()
    assert not setup_marker(wt).exists()  # a failed setup does not mark the worktree as prepared


def test_run_setup_timeout_kills_session_escaping_descendant(tmp_path):
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    child_pid = tmp_path / "child.pid"
    script = tmp_path / "setup.py"
    script.write_text(
        "import os, pathlib, signal\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f" pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        " os.close(1); os.close(2)\n"
        " signal.pause()\n"
        "signal.pause()\n"
    )

    with pytest.raises(RunnerError, match="timed out after 0.2s"):
        run_setup(wt, {
            "command": shlex.join([sys.executable, str(script)]), "timeout_seconds": 0.2,
        })

    assert child_pid.exists()
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 1
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not pid_alive(pid)


# ---- integration: local runner, checks, brief -------------------------------

def _garden_with_setup(garden, setup: dict) -> Store:
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = setup
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    return Store(garden)


def test_local_runner_runs_setup_with_env(garden, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    store = _garden_with_setup(garden, {"command": "echo prepared", "env": {"WIDGET_HOME": "/opt/widget"}})
    sc = Scheduler(store, github=fake_github, log=print)
    sc.tick()

    t = sc.store.task("DM-001")
    assert setup_marker(sc.worktree_for(t)).exists()
    run = sc.runs.latest("DM-001")
    assert (run.path / "setup.log").read_text().strip() == "prepared"

    # The worker runs in the prepared environment: launch the real LocalRunner with its
    # Popen stubbed out and look at the env it hands the process (nothing is started).
    import garden.runner.local as local_mod
    from garden.runner.local import LocalRunner

    launched: list[dict] = []
    monkeypatch.setattr(local_mod.subprocess, "Popen",
                        lambda *a, **k: launched.append(dict(k["env"])) or SimpleNamespace(pid=4242))
    LocalRunner({"setup": sc.cfg.product_setup("demo")}, sc.cfg.harness("claude")).start(run, sc.worktree_for(t), "brief")
    assert launched and launched[0].get("WIDGET_HOME") == "/opt/widget"
    assert launched[0]["GARDEN_TASK_ID"] == "DM-001"


def test_setup_failure_fails_the_run(garden, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    store = _garden_with_setup(garden, {"command": "echo could-not-prepare >&2; exit 5"})
    sc = Scheduler(store, github=fake_github, log=print)
    sc.tick()
    sc.store.invalidate()
    t = sc.store.task("DM-001")
    assert t.status == Status.FAILED
    assert "setup command failed" in "\n".join(t.body.splitlines())
    run = sc.runs.latest("DM-001")
    assert "could-not-prepare" in (run.path / "setup.log").read_text()


def test_setup_failure_marks_run_failed_not_leaked(garden, fake_github, monkeypatch):
    """A setup failure must mark its run failed, not leave it 'running' forever: a leaked run
    counts against active() and would permanently consume a max_parallel slot."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    store = _garden_with_setup(garden, {"command": "exit 5"})
    sc = Scheduler(store, github=fake_github, log=print)
    sc.tick()
    run = sc.runs.latest("DM-001")
    assert run.status == "failed" and run.error and run.finished_at
    assert sc.runs.active() == []  # slot freed, not leaked
    finished = [e for e in sc.events.read(task_id="DM-001", kinds=["run_finished"])
                if e.get("run") == run.run_id]
    assert len(finished) == 1 and finished[0]["status"] == "failed"


def test_pre_pr_checks_prepare_the_local_worktree(garden, fake_github):
    """The pre-PR checks run setup in the worktree first, so a remote run whose branch was just
    materialised locally (setup only ran on the host) still finds its prepared artifacts."""
    from garden import gitops

    store = _garden_with_setup(garden, {
        "command": "echo ready > .prepared",  # a fresh worktree has no .prepared until setup runs
        "test": "cat .prepared",
    })
    sc = Scheduler(store, github=fake_github, log=print)
    t = sc.store.task("DM-001")
    branch, base = "garden/dm-001-first-task", "main"
    wt = gitops.prepare_worktree(sc.repo_for(t), sc.worktree_for(t), branch, base)
    results = sc._pre_pr_checks(t, wt, branch, base)
    assert [(r["name"], r["status"]) for r in results] == [("test", "pass")]


def test_pre_pr_checks_report_setup_failure(garden, fake_github):
    from garden import gitops

    store = _garden_with_setup(garden, {"command": "echo boom >&2; exit 3", "test": "true"})
    sc = Scheduler(store, github=fake_github, log=print)
    t = sc.store.task("DM-001")
    branch, base = "garden/dm-001-first-task", "main"
    wt = gitops.prepare_worktree(sc.repo_for(t), sc.worktree_for(t), branch, base)
    results = sc._pre_pr_checks(t, wt, branch, base)
    assert results == [{"name": "setup", "status": "fail", "summary": "setup command failed",
                        "details": results[0]["details"], "origin": "infrastructure",
                        "exit_code": 3, "unavailable": False}]
    assert "setup command failed" in results[0]["details"]


def test_pre_pr_defaults_to_test_and_lint(garden, fake_github):
    store = _garden_with_setup(garden, {
        "test": "make test", "lint": "make lint", "env": {"WIDGET_HOME": "/opt/widget"},
    })
    sc = Scheduler(store, github=fake_github, log=print)
    specs = sc._pre_pr_specs(sc.store.task("DM-001"))
    assert [(s["name"], s["command"]) for s in specs] == [("test", "make test"), ("lint", "make lint")]
    assert all(s["env"] == {"WIDGET_HOME": "/opt/widget"} for s in specs)  # prepared env applied


def test_pre_pr_explicit_checks_still_win_and_get_env(garden, fake_github):
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = {"test": "make test", "env": {"WIDGET_HOME": "/opt/widget"}}
    cfg["checks"] = {"pre_pr": [{"name": "custom", "command": "true"}]}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    specs = sc._pre_pr_specs(sc.store.task("DM-001"))
    assert [s["name"] for s in specs] == ["custom"]  # explicit list is not replaced by test/lint
    assert specs[0]["env"] == {"WIDGET_HOME": "/opt/widget"}


def test_product_check_overrides_keep_each_product_validation_contract(garden, fake_github):
    """A lightweight product can opt out without changing the compiled product's checks."""
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["checks"] = {
        "pre_pr": [{"name": "compiled", "command": "make verify"}],
        "ci": [{"name": "ci-log", "command": "analyse-ci"}],
        "timeout_seconds": 900,
    }
    cfg["products"]["demo"]["setup"] = {"env": {"BUILD_KIND": "compiled"}}
    cfg["products"]["handbook"] = {
        "repo": ".",
        "setup": {"test": "make docs", "env": {"BUILD_KIND": "docs"}},
        "checks": {"pre_pr": [], "ci": [], "timeout_seconds": 45},
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)

    compiled = sc._check_settings(sc.store.task("DM-001"))
    handbook_task = SimpleNamespace(product="handbook", body="", extra={})
    handbook = sc._check_settings(handbook_task)

    assert compiled["specs"] == [{"name": "compiled", "command": "make verify",
                                   "env": {"BUILD_KIND": "compiled"}}]
    assert compiled["timeout"] == 900
    assert sc._check_settings(sc.store.task("DM-001"), "ci")["specs"][0]["name"] == "ci-log"
    assert handbook["specs"] == []  # explicit [] also suppresses setup.test fallback
    assert sc._check_settings(handbook_task, "ci")["specs"] == []
    assert handbook["timeout"] == 45


def test_ci_analyzers_exclude_required_pre_pr_checks(garden, fake_github):
    """Required check evidence extends the pre-PR gate, never the CI analyser contract."""
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["checks"] = {
        "pre_pr": [{"name": "unit", "command": "make test"}],
        "ci": [{"name": "ci-log", "command": "analyse-ci"}],
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    task = sc.store.task("DM-001")
    task.extra["requires"] = ["check: unit"]

    assert [spec["name"] for spec in sc._check_settings(task)["specs"]] == ["unit"]
    assert [spec["name"] for spec in sc._check_settings(task, "ci")["specs"]] == ["ci-log"]


def test_check_run_persists_resolved_product_settings(garden, fake_github):
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["checks"] = {"pre_pr": [{"name": "global", "command": "true"}], "ci": [], "timeout_seconds": 900}
    cfg["products"]["demo"]["checks"] = {
        "pre_pr": [{"name": "product", "command": "true", "env": {"CHECK_LEVEL": "product"}}],
        "timeout_seconds": 45,
    }
    cfg["products"]["demo"]["setup"] = {"env": {"CHECK_LEVEL": "setup", "KEEP": "yes"}}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    task = sc.store.task("DM-001")
    settings = sc._check_settings(task)

    assert settings["specs"][0]["env"] == {"CHECK_LEVEL": "product", "KEEP": "yes"}
    # The detached job's durable payload, and the continuation used by probes/retries, retain
    # the same product-specific command and timeout after garden.yaml later changes.
    from garden import gitops
    from garden.scheduler.report import TickReport
    worktree = gitops.prepare_worktree(sc.repo_for(task), sc.worktree_for(task), "garden/dm-001", "main")
    run = sc._dispatch_check_run(task, worktree=worktree, branch="garden/dm-001", base="main",
                                 specs=settings["specs"], stage="pre_pr", cont={}, rep=TickReport())
    payload = yaml.safe_load((run.path / "checks_input.json").read_text())
    continuation = sc.state.get(task.id)["check_run"]["cont"]["check_settings"]
    assert payload["timeout"] == continuation["timeout"] == 45
    assert payload["specs"] == continuation["specs"] == settings["specs"]
    assert payload["config"] == continuation["config"]
    sc.cfg.data["checks"]["timeout_seconds"] = 1
    sc.cfg.data["products"]["demo"]["checks"]["pre_pr"] = []
    assert continuation["timeout"] == 45
    assert continuation["specs"][0]["name"] == "product"

    # A base probe may run only failed checks, but a successful rebase must return to the
    # complete contract instead of silently shrinking its next validation.
    full_specs = [*settings["specs"], {"name": "second", "command": "true"}]
    continuation["specs"] = full_specs
    probe = sc._dispatch_check_run(task, worktree=worktree, branch="garden/dm-001", base="main",
                                   specs=[full_specs[0]], stage="base_probe",
                                   cont={"check_settings": continuation}, rep=TickReport())
    probe_payload = yaml.safe_load((probe.path / "checks_input.json").read_text())
    probe_continuation = sc.state.get(task.id)["check_run"]["cont"]["check_settings"]
    assert [spec["name"] for spec in probe_payload["specs"]] == ["product"]
    assert [spec["name"] for spec in probe_continuation["specs"]] == ["product", "second"]


def test_check_cli_pre_pr_uses_the_resolver(garden):
    """`garden check ID` for pre_pr goes through the same resolver as the automated gate:
    it falls back to setup.test/setup.lint (no more 'no checks configured'), merges setup.env,
    and uses the product's timeout, so the manual command agrees with the scheduler."""
    import os

    from typer.testing import CliRunner

    from garden.cli import app

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = {"test": "true", "lint": "true"}  # no explicit checks.pre_pr
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        r = CliRunner().invoke(app, ["check", "DM-001"])
    finally:
        os.chdir(cwd)
    assert r.exit_code == 0, r.output
    assert "no checks configured" not in r.output
    assert "test" in r.output and "lint" in r.output


def test_brief_states_test_and_lint_and_never_names_a_venv(garden):
    store = _garden_with_setup(garden, {"test": "npm test", "lint": "npm run lint"})
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main")
    assert "already prepared" in b.text
    assert "`npm test` (tests)" in b.text and "`npm run lint` (lint)" in b.text
    lowered = b.text.lower()
    assert "venv" not in lowered and "pip install" not in lowered and " uv " not in lowered


def test_brief_env_rule_without_commands(garden):
    b = build_brief(Store(garden), Store(garden).task("DM-001"), branch="garden/x", base="main")
    assert "already prepared" in b.text  # still tells the worker not to install, even with no commands


def test_targeted_checks_never_leak_the_products_unconfigured_full_suite_command(garden):
    """CGS-012 criterion 7: whether this is an initial work brief or a revision (review_feedback
    set), the run's actual checks are what gets named — never the product's own broader
    setup.test/lint, and never a browser-backed command that isn't one of those checks."""
    store = _garden_with_setup(garden, {"test": "pytest -q", "lint": "npx playwright test"})
    targeted = [{"name": "focused", "command": "pytest tests/test_widget.py -q"}]
    for review_feedback in ("", "tighten the widget test"):
        b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main",
                        review_feedback=review_feedback, checks=targeted)
        assert "`pytest tests/test_widget.py -q` (focused)" in b.text
        assert "pytest -q" not in b.text
        assert "npx playwright test" not in b.text


@pytest.mark.needs_remote_clone
def test_ssh_runner_runs_setup_on_host(garden, fake_github):
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = {"command": "npm ci", "env": {"WIDGET_HOME": "/opt/widget"}}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    t = sc.store.task("DM-001")
    t.runner = "ssh"
    sc.store.save(t)
    sc.tick()
    remote_sh = (sc.runs.latest("DM-001").path / "remote.sh").read_text()
    assert "export WIDGET_HOME=/opt/widget" in remote_sh
    assert "GARDEN_SETUP_CMD='npm ci'" in remote_sh
    assert "GARDEN_SETUP_MARKER=" in remote_sh


@pytest.mark.needs_remote_clone
def test_ssh_setup_honors_timeout(garden, fake_github):
    """The remote setup command is wrapped with the configured setup timeout (when `timeout` is
    on the host), not left to run until the much larger whole-run limit."""
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = {"command": "npm ci", "timeout_seconds": 123}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    t = sc.store.task("DM-001")
    t.runner = "ssh"
    sc.store.save(t)
    sc.tick()
    remote_sh = (sc.runs.latest("DM-001").path / "remote.sh").read_text()
    assert "GARDEN_SETUP_TIMEOUT=123" in remote_sh
    assert 'timeout $GARDEN_SETUP_TIMEOUT sh -c' in remote_sh


@pytest.mark.needs_remote_clone
def test_ssh_host_setup_override(garden, fake_github):
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["products"]["demo"]["setup"] = {"command": "npm ci", "env": {"A": "1"}}
    cfg["ssh"]["hosts"][0]["setup"] = {"command": "company-bootstrap", "env": {"B": "2"}}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    sc = Scheduler(Store(garden), github=fake_github, log=print)
    t = sc.store.task("DM-001")
    t.runner = "ssh"
    sc.store.save(t)
    sc.tick()
    remote_sh = (sc.runs.latest("DM-001").path / "remote.sh").read_text()
    assert "GARDEN_SETUP_CMD=company-bootstrap" in remote_sh  # host command overrides product
    assert "export A=1" in remote_sh and "export B=2" in remote_sh  # env merges
