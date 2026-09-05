"""`garden canary` (CG-180): install a pinned build into a throwaway venv and drive it end to
end — the scripted QA flows plus a stacked-PR and a merge-queue scenario against an in-memory
GitHub that reports a real check latency and closes a child whose base branch was deleted."""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden import canary
from garden import runner as runner_registry
from garden.cli import app
from garden.runner.local import LocalRunner


@pytest.fixture(autouse=True)
def real_local_runner(monkeypatch):
    """Like `garden qa`, the canary drives the real loop with real (token-free) worker
    processes, so it needs the real local runner, not the suite's in-process one."""
    monkeypatch.setitem(runner_registry.REGISTRY, "local", LocalRunner)
    monkeypatch.setitem(runner_registry.REGISTRY, "claude-local", LocalRunner)


def run(cwd, *args):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(old)


def test_scenarios_pass_on_a_good_build(tmp_path):
    rows = canary.run_scenarios(tmp_path / "scenarios")
    assert [r["name"] for r in rows] == ["stacked child survives the parent's merge",
                                         "merge queue merges through a pending rollup"]
    assert all(r["ok"] for r in rows), rows


def test_self_check_passes_on_the_current_build(tmp_path):
    r = run(tmp_path, "canary", "--skip-install", "--out", str(tmp_path / "canary"))
    assert r.exit_code == 0, r.output
    assert "every check passed" in r.output
    assert "scripted QA flows" in r.output
    assert "stacked child survives" in r.output and "merge queue merges" in r.output


def test_exits_non_zero_when_a_scenario_fails(tmp_path, monkeypatch):
    """A regression in the build must make the canary fail. Simulate one by having a scenario
    report a failure (a real broken build fails the same way: e.g. the child is orphaned)."""
    def broken_stacked(root, log):
        return {"name": "stacked child survives the parent's merge", "ok": False,
                "detail": "the child PR is closed after the parent merged (orphaned by the base deletion)"}

    monkeypatch.setattr(canary, "_scenario_stacked", broken_stacked)
    r = run(tmp_path, "canary", "--skip-install", "--out", str(tmp_path / "canary"))
    assert r.exit_code == 1, r.output
    assert "canary: FAILED" in r.output
    assert "FAIL stacked child survives" in r.output


def test_run_canary_reports_an_install_failure(tmp_path, monkeypatch):
    """When the pin will not install, the canary fails before any scenario runs."""
    monkeypatch.setattr(canary, "install_build", lambda url, sha, venv, log=None: (False, Path("x"), "pip: no such ref"))
    report = canary.run_canary("deadbeef", url="/some/repo", out=tmp_path / "canary")
    assert not report.ok and not report.install_ok
    assert "install failed" in report.install_error
    assert (tmp_path / "canary" / "install.log").read_text() == "pip: no such ref"


def test_run_canary_needs_a_url_for_a_pinned_build(tmp_path):
    report = canary.run_canary("deadbeef", url="", out=tmp_path / "canary")
    assert not report.ok and "no install URL" in report.install_error


def test_canary_needs_a_build_to_check(tmp_path):
    r = run(tmp_path, "canary")  # no sha, no --skip-install, no live garden
    assert r.exit_code == 2
    assert "no build to check" in r.output
