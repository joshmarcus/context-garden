import json

import yaml
from typer.testing import CliRunner

from garden.cli import app

runner = CliRunner()


def run(garden, *args):
    import os

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_status_ls_graph_validate(garden):
    r = run(garden, "status")
    assert r.exit_code == 0, r.output
    r = run(garden, "ls", "--json")
    data = json.loads(r.output)
    assert {d["id"] for d in data} == {"DM-001", "DM-002"}
    assert next(d for d in data if d["id"] == "DM-002")["effective_status"] == "blocked"
    r = run(garden, "graph", "--format", "mermaid")
    assert "DM_001 --> DM_002" in r.output
    assert run(garden, "validate").exit_code == 0


def test_status_shows_retro_waiting_for_personas(garden):
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    ph = sched.store.phase("demo", "p1")
    entry = {"phase": ph.key, "product": ph.product, "phase_name": ph.name,
             "personas": ["designer", "security", "user"], "skip_personas": False,
             "next_phase": "p2", "self_product": "demo", "stage": "personas", "persona_runs": {}}
    sched._retro_list().append(entry)
    sched.state.save()

    r = run(garden, "status")
    assert r.exit_code == 0, r.output
    assert "demo/p1 retro: waiting for personas (0 of 3)" in r.output


def test_set_and_clear_max_parallel(garden):
    r = run(garden, "status")
    assert r.exit_code == 0 and "workers: 0/2" in r.output and "live override" not in r.output

    r = run(garden, "set", "max_parallel", "5")
    assert r.exit_code == 0 and "max_parallel = 5" in r.output

    r = run(garden, "status")
    assert r.exit_code == 0 and "workers: 0/5 (live override; garden.yaml: 2)  reviews: 0/5" in r.output

    r = run(garden, "clear", "max_parallel")
    assert r.exit_code == 0 and "max_parallel override cleared" in r.output

    r = run(garden, "status")
    assert r.exit_code == 0 and "workers: 0/2" in r.output and "live override" not in r.output


def test_set_rejects_unknown_key(garden):
    r = run(garden, "set", "auto_dispatch", "false")
    assert r.exit_code != 0
    assert "can't be set live" in r.output


def test_trellis_open_filter(garden):
    assert run(garden, "set-status", "DM-001", "done").exit_code == 0
    r = run(garden, "trellis")
    lines = [line for line in r.output.splitlines() if line.strip()]
    assert any(line.startswith("DM-001") for line in lines)
    assert any(line.startswith("DM-002") for line in lines)

    r = run(garden, "trellis", "--open")
    lines = [line for line in r.output.splitlines() if line.strip()]
    assert not any(line.startswith("DM-001") for line in lines)
    assert any(line.startswith("DM-002") for line in lines)

    r = run(garden, "trellis", "--format", "json", "--open")
    data = json.loads(r.output)
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"DM-002"}
    assert data["edges"] == []

    r = run(garden, "trellis", "--format", "mermaid", "--open")
    assert "DM_001" not in r.output and "DM_002" in r.output


def test_terminal_task_actions_are_refused_and_set_status_needs_force(garden):
    """CG-142: done/cancelled are terminal on the CLI too. `garden set-status` is the only
    escape hatch, and it needs --force to move a task back out of one of them."""
    assert run(garden, "pr", "DM-001", "https://github.com/test/demo/pull/71").exit_code == 0
    assert run(garden, "set-status", "DM-001", "done").exit_code == 0

    for args in (("retry", "DM-001"), ("cancel", "DM-001"), ("review", "DM-001"), ("dispatch", "DM-001"),
                 ("resume", "DM-001")):
        r = run(garden, *args)
        assert r.exit_code == 1, args
        assert "DM-001 is done" in r.output, args

    r = run(garden, "set-status", "DM-001", "ready")
    assert r.exit_code == 1 and "use --force" in r.output

    r = run(garden, "set-status", "DM-001", "ready", "--force")
    assert r.exit_code == 0 and "DM-001 -> ready" in r.output


def test_new_task_and_approve(garden):
    r = run(garden, "new-task", "demo/p1", "Third: thing", "--dep", "DM-001", "--read", "demo/p1/specs/spec.md")
    assert r.exit_code == 0 and "DM-003" in r.output
    r = run(garden, "approve", "DM-003")
    assert "DM-003 -> ready" in r.output
    r = run(garden, "show", "DM-003")
    assert "Third: thing" in r.output and "ready" in r.output


def test_budget_command(garden):
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    r = run(garden, "budget", "demo/p1", "50")
    assert r.exit_code == 0 and "$50.00" in r.output
    assert Scheduler(Store(garden), github=FakeGitHub()).budget_for("demo/p1") == 50.0
    r = run(garden, "budget", "demo/p1", "none")
    assert r.exit_code == 0 and "no cap" in r.output
    assert Scheduler(Store(garden), github=FakeGitHub()).budget_for("demo/p1") == 0.0
    # bad inputs
    assert run(garden, "budget", "demo/nope", "10").exit_code == 1
    assert run(garden, "budget", "demo/p1", "abc").exit_code == 1
    assert run(garden, "budget", "noslash", "10").exit_code == 1


def test_suggest_command(garden):
    r = run(garden, "suggest", "DM-001", "acceptance should cover the empty case", "--by", "josh", "--applies-to", "acceptance")
    assert r.exit_code == 0, r.output
    r = run(garden, "show", "DM-001", "--raw")
    assert "## Suggestions" in r.output and "acceptance should cover the empty case" in r.output


def test_brief_stats(garden):
    r = run(garden, "brief", "DM-001", "--stats")
    assert r.exit_code == 0 and "tokens" in r.output


def test_usage_phase_header_and_rows_with_no_runs(garden):
    r = run(garden, "usage", "demo/p1")
    assert r.exit_code == 0, r.output
    assert "fixed brief cost" in r.output
    assert "DM-001" in r.output and "DM-002" in r.output


def test_plan_import(garden, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps([{"title": "Imported", "body": "## Goal\n\nx"}]))
    r = run(garden, "plan", "demo/p1", "--import", str(f))
    assert r.exit_code == 0 and "Imported" in r.output


def test_runs_shows_unreaped_finished_run(garden, monkeypatch):
    """CG-083: a run whose record is done while its task is still `running` (an
    interrupted reap) shows up as finished-but-unreaped, not as a plain `done`
    row that looks identical to a properly reaped run."""
    from garden.model import Status
    from garden.runs import RunStore
    from garden.store import Store

    rs = RunStore(garden / ".garden")
    r0 = rs.new_run("DM-001", "local", "work")
    r0.status = "done"
    r0.finished_at = "2026-01-01T00:00:00+00:00"
    r0.save()

    store = Store(garden)
    t = store.task("DM-001")
    t.status = Status.RUNNING
    store.save(t)

    monkeypatch.setenv("COLUMNS", "200")  # wide enough that Rich doesn't wrap/truncate the status cell
    r = run(garden, "runs")
    assert r.exit_code == 0, r.output
    assert "finished, not yet reaped" in " ".join(r.output.split())


def test_friction(garden):
    from garden.runs import RunStore

    rs = RunStore(garden / ".garden")
    r0 = rs.new_run("DM-001", "manual", "work")
    r0.status = "done"
    r0.result = {
        "status": "done",
        "pr_body": "## Summary\n\nDid the thing.\n\n## Friction\n\nNo docs for the X module.\n\n## Notes\n\nAll good.",
    }
    r0.save()

    r = run(garden, "friction", "demo/p1")
    assert r.exit_code == 0, r.output
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "DM-001" in text
    assert "No docs for the X module." in text

    # Running again produces identical output (idempotent)
    r2 = run(garden, "friction", "demo/p1")
    assert r2.exit_code == 0
    assert doc.read_text() == text


def test_friction_no_github_fallback_needed(garden):
    """Tasks without runs produce an empty friction file."""
    r = run(garden, "friction", "demo/p1")
    assert r.exit_code == 0, r.output
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    assert "No friction reported yet." in doc.read_text()


def test_friction_report_cli(garden):
    r = run(garden, "friction-report", "demo/p1", "The new-task flow is too many steps.")
    assert r.exit_code == 0, r.output
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "## Reported" in text
    assert "The new-task flow is too many steps." in text
    # A draft task should have been created
    assert "DM-003" in r.output or "created draft task" in r.output


def test_friction_report_preserves_harvested(garden):
    """friction (harvester) run after friction-report keeps the Reported section."""
    from garden.runs import RunStore

    rs = RunStore(garden / ".garden")
    r0 = rs.new_run("DM-001", "manual", "work")
    r0.status = "done"
    r0.result = {"status": "done", "pr_body": "## Friction\n\nHard to find logs."}
    r0.save()

    # File a report
    run(garden, "friction-report", "demo/p1", "Provenance is missing from error messages.")
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert "## Reported" in doc.read_text()

    # Now run the harvester
    r = run(garden, "friction", "demo/p1")
    assert r.exit_code == 0, r.output
    text = doc.read_text()
    assert "Hard to find logs." in text          # harvested friction preserved
    assert "## Reported" in text                  # reported section preserved
    assert "Provenance is missing" in text         # report preserved


def test_init_scaffold(tmp_path):
    r = runner.invoke(app, ["init", str(tmp_path / "g"), "--name", "x"])
    assert r.exit_code == 0 and (tmp_path / "g" / "garden.yaml").exists()

    skills = tmp_path / "g" / ".claude" / "skills"
    for slug in ("garden-take", "garden-plan", "garden-review", "garden-operate"):
        assert (skills / slug / "SKILL.md").exists()
    operate = (skills / "garden-operate" / "SKILL.md").read_text()
    assert "joshmarcus/context-garden" in operate

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path / "g")
    try:
        r = runner.invoke(app, ["new-product", "widget", "--repo", "../widget"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["new-phase", "widget", "phase-01"])
        assert r.exit_code == 0 and (tmp_path / "g" / "widget" / "phase-01" / "goals.md").exists()
    finally:
        os.chdir(cwd)


def test_doctor_success_with_valid_setup(garden, monkeypatch):
    import subprocess
    from unittest import mock

    with mock.patch("subprocess.run") as mock_run:
        def side_effect(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                cmd_str = " ".join(cmd)
                if "config" in cmd and "git" in cmd:
                    if "user.email" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                    elif "user.name" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
                elif "auth" in cmd and "status" in cmd:
                    if "gh" in cmd_str or "claude" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                elif "api" in cmd and "user" in cmd and "gh" in cmd_str:
                    return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
            raise RuntimeError(f"Unexpected subprocess.run call: {cmd}")

        mock_run.side_effect = side_effect
        r = run(garden, "doctor")
        assert r.exit_code == 0, r.output
        assert "all good" in r.output


def test_doctor_fails_with_no_gh_login(garden, monkeypatch):
    import subprocess
    from unittest import mock

    with mock.patch("subprocess.run") as mock_run:
        def side_effect(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                cmd_str = " ".join(cmd)
                if "auth" in cmd and "status" in cmd:
                    if "gh" in cmd_str:
                        raise subprocess.CalledProcessError(1, cmd)
                    if "claude" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                if "git" in cmd and "config" in cmd:
                    if "user.email" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                    elif "user.name" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
            raise RuntimeError(f"Unexpected subprocess.run call: {cmd}")

        mock_run.side_effect = side_effect
        r = run(garden, "doctor")
        assert r.exit_code == 1, r.output
        # Collapse whitespace: Rich wraps the line at the console width, which can split
        # "NOT LOGGED IN" across a newline depending on how long the preceding path is.
        assert "NOT LOGGED IN" in " ".join(r.output.split())


def test_doctor_fails_with_no_harness_login(garden, monkeypatch):
    import subprocess
    from unittest import mock

    with mock.patch("subprocess.run") as mock_run:
        def side_effect(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                cmd_str = " ".join(cmd)
                if "auth" in cmd and "status" in cmd:
                    if "claude" in cmd_str:
                        raise subprocess.CalledProcessError(1, cmd)
                    if "gh" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                if "git" in cmd and "config" in cmd:
                    if "user.email" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                    elif "user.name" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
                if "api" in cmd and "user" in cmd and "gh" in cmd_str:
                    return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
            raise RuntimeError(f"Unexpected subprocess.run call: {cmd}")

        mock_run.side_effect = side_effect
        r = run(garden, "doctor")
        assert r.exit_code == 1, r.output
        # Collapse whitespace: the harness line embeds the (long) fake_claude path, so Rich
        # wraps at the console width and can split "NOT LOGGED IN" across a newline.
        assert "NOT LOGGED IN" in " ".join(r.output.split())


def test_doctor_fails_with_no_git_identity(garden, monkeypatch):
    import subprocess
    from unittest import mock

    with mock.patch("subprocess.run") as mock_run:
        def side_effect(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                cmd_str = " ".join(cmd)
                if "git" in cmd and "config" in cmd:
                    raise subprocess.CalledProcessError(1, cmd)
                if "auth" in cmd and "status" in cmd:
                    if "claude" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                    if "gh" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                if "api" in cmd and "user" in cmd and "gh" in cmd_str:
                    return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
            raise RuntimeError(f"Unexpected subprocess.run call: {cmd}")

        mock_run.side_effect = side_effect
        r = run(garden, "doctor")
        assert r.exit_code == 1, r.output
        assert "missing user.name or user.email" in r.output


def test_doctor_reports_a_clone_missing_git_identity(garden, monkeypatch):
    """CG-147: `garden doctor` walks every clone under work_dir/repos/, not just the checkout
    it happens to run from, so a clone made without an identity is caught before a worker or
    the scheduler hits "Author identity unknown" on its first commit."""
    import subprocess
    from unittest import mock

    from garden.store import Store

    store = Store(garden)
    clone = store.config.repos_dir / "some-product"
    (clone / ".git").mkdir(parents=True)

    with mock.patch("subprocess.run") as mock_run:
        def side_effect(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                cmd_str = " ".join(cmd)
                if "config" in cmd and "git" in cmd:
                    if kwargs.get("cwd") == clone:
                        return subprocess.CompletedProcess(cmd, 1, stdout="")
                    if "user.email" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                    elif "user.name" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
                elif "auth" in cmd and "status" in cmd:
                    if "gh" in cmd_str or "claude" in cmd_str:
                        return subprocess.CompletedProcess(cmd, 0)
                elif "api" in cmd and "user" in cmd and "gh" in cmd_str:
                    return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
            raise RuntimeError(f"Unexpected subprocess.run call: {cmd}")

        mock_run.side_effect = side_effect
        r = run(garden, "doctor")
        assert r.exit_code == 1, r.output
        assert "some-product" in r.output
        assert "missing git identity" in r.output


def test_doctor_tests_notify_command(garden, monkeypatch):
    """`garden doctor` actually runs a configured notify.command with a synthetic payload,
    rather than just reporting whether the key is set."""
    import subprocess
    from unittest import mock

    (garden / "garden.local.yaml").write_text(yaml.safe_dump({"notify": {"command": "exit 0"}}))

    def side_effect(cmd, *args, **kwargs):
        if isinstance(cmd, str):
            assert kwargs.get("env", {}).get("GARDEN_TASK_ID") == "DOCTOR-TEST"
            return subprocess.CompletedProcess(cmd, 0)
        if isinstance(cmd, list):
            if "config" in cmd and "git" in cmd:
                if "user.email" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                elif "user.name" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
            elif "auth" in cmd and "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            elif "api" in cmd and "user" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
        raise RuntimeError(f"Unexpected subprocess call: {cmd}")

    with mock.patch("subprocess.run", side_effect=side_effect):
        r = run(garden, "doctor")
        assert r.exit_code == 0, r.output
        assert "notify" in r.output and "test ok" in r.output


def test_doctor_flags_a_failing_notify_command(garden, monkeypatch):
    import subprocess
    from unittest import mock

    (garden / "garden.local.yaml").write_text(yaml.safe_dump({"notify": {"command": "exit 1"}}))

    def side_effect(cmd, *args, **kwargs):
        if isinstance(cmd, str):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        if isinstance(cmd, list):
            if "config" in cmd and "git" in cmd:
                if "user.email" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="test@example.com\n")
                elif "user.name" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="Test User\n")
            elif "auth" in cmd and "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            elif "api" in cmd and "user" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="testuser\n")
        raise RuntimeError(f"Unexpected subprocess call: {cmd}")

    with mock.patch("subprocess.run", side_effect=side_effect):
        r = run(garden, "doctor")
        assert r.exit_code == 1, r.output
        assert "notify" in r.output and "failed" in r.output


def test_priority_and_difficulty_commands(garden):
    from garden.store import Store

    assert run(garden, "priority", "DM-001", "0").exit_code == 0
    assert run(garden, "difficulty", "DM-001", "hard").exit_code == 0
    assert run(garden, "difficulty", "DM-001", "extreme").exit_code == 1
    t = Store(garden).task("DM-001")
    assert t.priority == 0 and t.difficulty == "hard"
    assert "difficulty medium -> hard" in t.body
