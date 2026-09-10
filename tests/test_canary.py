"""`garden canary` (CG-180): install a pinned build into a throwaway venv and drive it end to
end — the scripted QA flows plus a stacked-PR and a merge-queue scenario against an in-memory
GitHub that reports a real check latency and closes a child whose base branch was deleted."""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden import canary, qa
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


def _passing_qa_report(out):
    return qa.QAReport(out=out, result={}, findings=[], flows=[
        {"name": flow.name, "ok": True, "page": flow.page, "note": ""}
        for flow in qa.FLOWS
    ])


def test_self_check_composes_injected_results(tmp_path, monkeypatch):
    calls = []

    def fake_qa(out, *, scripted, log):
        calls.append(("qa", out, scripted))
        return _passing_qa_report(out)

    scenario_rows = [
        {"name": "stacked child survives the parent's merge", "ok": True, "detail": "retargeted"},
        {"name": "merge queue merges through a pending rollup", "ok": True, "detail": "settled"},
    ]

    def fake_scenarios(out, log):
        calls.append(("scenarios", out))
        return scenario_rows

    monkeypatch.setattr(qa, "run_qa", fake_qa)
    monkeypatch.setattr(canary, "run_scenarios", fake_scenarios)

    r = run(tmp_path, "canary", "--skip-install", "--out", str(tmp_path / "canary"))
    assert r.exit_code == 0, r.output
    assert "every check passed" in r.output
    assert "scripted QA flows" in r.output
    assert "stacked child survives" in r.output and "merge queue merges" in r.output
    assert [call[0] for call in calls] == ["qa", "scenarios"]
    assert calls[0][2] is True


@pytest.mark.parametrize(
    "scenario_rows, diagnostic",
    [
        (
            [{"name": "stacked child survives the parent's merge", "ok": False,
              "detail": "the child PR was orphaned"}],
            "the child PR was orphaned",
        ),
        (
            [{"name": "stacked child survives the parent's merge"}],
            "FAIL stacked child survives",
        ),
    ],
    ids=["scenario-failure", "malformed-scenario-result"],
)
def test_exits_non_zero_for_injected_scenario_result(tmp_path, monkeypatch, scenario_rows, diagnostic):
    """Injected orchestration results retain canary failure diagnostics and exit semantics."""
    monkeypatch.setattr(qa, "run_qa", lambda out, *, scripted, log: _passing_qa_report(out))
    monkeypatch.setattr(canary, "run_scenarios", lambda out, log: scenario_rows)

    r = run(tmp_path, "canary", "--skip-install", "--out", str(tmp_path / "canary"))
    assert r.exit_code == 1, r.output
    assert "canary: FAILED" in r.output
    assert diagnostic in r.output


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


def test_drive_never_bypasses_the_config_reload_gate():
    """_drive() ticks a real scheduler in a loop; garden.yaml reload is gated inside tick()
    itself (CG-242), so between ticks it must only re-scan task files (invalidate_tasks), never
    call the unconditional invalidate() — that would adopt a held executable-field change
    (e.g. a worker's own notify.command write) before the fence gets to reap and revert it."""

    class FakeStore:
        def __init__(self):
            self.invalidate_calls = 0
            self.invalidate_tasks_calls = 0

        def invalidate(self):
            self.invalidate_calls += 1

        def invalidate_tasks(self):
            self.invalidate_tasks_calls += 1

    class FakeScheduler:
        def __init__(self):
            self.store = FakeStore()
            self.ticks = 0

        def tick(self):
            self.ticks += 1

    sched = FakeScheduler()
    assert canary._drive(sched, lambda: sched.ticks >= 3, timeout=5, interval=0)
    assert sched.store.invalidate_calls == 0
    assert sched.store.invalidate_tasks_calls == 3
