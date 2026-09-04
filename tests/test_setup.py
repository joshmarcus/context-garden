"""Per-product environment setup: `products.<name>.setup` prepares a fresh worktree
(command + env), names the test/lint commands, and nothing assumes Python/pip/uv/a venv."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from garden.brief import build_brief
from garden.model import Status
from garden.runner.base import RunnerError, run_setup, setup_marker
from garden.scheduler import Scheduler
from garden.store import Store
from tests.conftest import wait_for_runs

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

    captured: list[dict] = []
    import garden.runner.local as local_mod
    orig = subprocess.Popen

    def spy(*args, **kwargs):
        if kwargs.get("env") is not None:
            captured.append(dict(kwargs["env"]))
        return orig(*args, **kwargs)

    monkeypatch.setattr(local_mod.subprocess, "Popen", spy)
    sc.tick()
    wait_for_runs(sc)

    t = sc.store.task("DM-001")
    assert setup_marker(sc.worktree_for(t)).exists()
    worker_env = [e for e in captured if "GARDEN_TASK_ID" in e]
    assert worker_env and worker_env[0].get("WIDGET_HOME") == "/opt/widget"
    run = sc.runs.latest("DM-001")
    assert (run.path / "setup.log").read_text().strip() == "prepared"


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
    assert run.status == "failed" and run.error
    assert sc.runs.active() == []  # slot freed, not leaked


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
                        "details": results[0]["details"]}]
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


def test_check_cli_pre_pr_uses_the_resolver(garden):
    """`garden check ID` for pre_pr goes through the same resolver as the automated gate:
    it falls back to setup.test/setup.lint (no more 'no checks configured') and merges setup.env,
    so the manual command agrees with what the scheduler runs."""
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
