import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from garden.model import Status
from garden.runner.local import LocalRunner
from garden.runner.manual import ManualRunner


def _wait_for_child(run) -> None:
    """The ssh runner is the one path in the suite that still launches a real command: its
    remote script is shell, so fake_ssh runs it with `sh`, which runs fake_claude as a
    process. Wait on that child directly rather than polling for its exit_code: no sleep,
    no timeout. A ChildProcessError means subprocess's own bookkeeping already reaped it,
    and the wrapper writes exit_code before it exits."""
    try:
        os.waitpid(run.pid, 0)
    except ChildProcessError:
        pass
    assert (run.path / "exit_code").exists()


def test_codex_harness_and_difficulty_model(sched, garden, fake_github):
    t = sched.store.task("DM-001")
    t.harness = "codex"
    t.difficulty = "hard"
    sched.store.save(t)
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.harness == "codex" and run.model == "gpt-max"
    sched.tick()
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    run = sched.runs.latest("DM-001")
    assert run.usage["input_tokens"] == 450 and run.result["summary"] == "codex did it with gpt-max"
    assert fake_github.created[0]["title"] == "Codex PR"


def test_codex_run_prices_cost_from_usage(sched, garden, fake_github):
    t = sched.store.task("DM-001")
    t.harness = "codex"
    t.model = "gpt-5.6-terra"
    sched.store.save(t)
    sched.tick()
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.model == "gpt-5.6-terra"
    # fake_codex reports {input_tokens: 500, cached_input_tokens: 50, output_tokens: 80}
    expected = (450 * 2.0 + 50 * 0.2 + 80 * 12.0) / 1_000_000
    assert run.cost_usd == pytest.approx(expected)
    finished = [e for e in sched.events.read() if e.get("kind") == "run_finished" and e.get("task") == "DM-001"]
    assert finished[-1]["cost_usd"] == pytest.approx(expected)
    assert finished[-1]["model"] == "gpt-5.6-terra"


def test_codex_run_with_unpriced_model_leaves_cost_null_and_logs(sched, garden, fake_github, capsys):
    t = sched.store.task("DM-001")
    t.harness = "codex"
    t.model = "totally-custom-model"
    sched.store.save(t)
    sched.tick()
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.model == "totally-custom-model" and run.cost_usd is None
    assert run.usage.get("input_tokens") == 450
    out = capsys.readouterr().out
    assert "no price configured for model 'totally-custom-model'" in out


def test_explicit_model_override(sched):
    t = sched.store.task("DM-001")
    t.model = "my-model"
    sched.store.save(t)
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.model == "my-model"
    assert (sched.worktree_for(t) / "model.txt").read_text().strip() == "my-model"


def test_easy_task_gets_cheap_model(sched):
    t = sched.store.task("DM-001")
    t.difficulty = "easy"
    sched.store.save(t)
    sched.tick()
    assert sched.runs.latest("DM-001").model == "haiku"


@pytest.mark.parametrize("harness, output", [("claude", "worker-output.txt"), ("codex", "codex-output.txt")])
def test_ssh_runner_end_to_end(sched, garden, fake_github, tmp_path, harness, output):
    t = sched.store.task("DM-001")
    t.runner = "ssh"
    t.harness = harness
    sched.store.save(t)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]
    run = sched.runs.latest("DM-001")
    assert run.runner == "ssh" and run.host == "boxA" and run.worktree == ""
    assert "git push" in (run.path / "remote.sh").read_text()
    _wait_for_child(run)
    assert (run.path / "exit_code").read_text().strip() == "0", (run.path / "stderr.log").read_text()
    rep = sched.tick()
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW, rep.summary()
    # the remote clone pushed the branch to origin, and a local worktree was materialised for review
    remote = tmp_path / "remote.git"
    out = subprocess.run(["git", "branch", "--list", "garden/*"], cwd=remote, capture_output=True, text=True, check=False).stdout
    assert "garden/dm-001-first-task" in out
    assert (sched.worktree_for(t) / output).exists()
    assert fake_github.created[0]["head"] == "garden/dm-001-first-task"


def test_local_runner_doctor_windows():
    with patch("os.name", "nt"):
        runner = LocalRunner({}, None)
        errors = runner.doctor()
    assert len(errors) == 1 and "WSL" in errors[0]


def test_local_runner_harness_shell_resolves_bin(tmp_path):
    from garden.harness import Harness
    from garden.runs import Run
    h = Harness("claude", {})
    runner = LocalRunner({}, h)
    fake = tmp_path / "claude-resolved"
    fake.touch()
    fake.chmod(0o755)
    run = Run(task_id="T-001", run_id="r1", dir=str(tmp_path), runner="local")
    with patch("shutil.which", side_effect=lambda name: str(fake) if name == "claude" else None):
        cmd = runner.harness_shell(run, tmp_path, None)
    assert cmd.startswith(str(fake))


def test_local_runner_probe_uses_the_minimal_login_probe_not_the_full_command(tmp_path):
    """The paused-harness probe (CG-212) must never grant edit/Bash permissions: it runs the
    same minimal, tool-less invocation `garden doctor`'s login check uses (Harness.login_probe),
    not the full `--permission-mode`/`--allowedTools`/`--settings` command a real dispatch
    builds."""
    from garden.harness import Harness

    h = Harness("claude", {})
    runner = LocalRunner({}, h)
    captured: dict = {}

    def fake_probe_launch(argv, stdin_text, cwd, env):
        captured["argv"] = argv
        captured["stdin_text"] = stdin_text
        return '{"type": "result", "subtype": "success", "is_error": false, "result": "ready"}', ""

    with patch.object(LocalRunner, "_probe_launch", side_effect=fake_probe_launch):
        result = runner.probe(tmp_path / "probe" / "claude")
    argv = captured["argv"]
    assert "--permission-mode" not in argv
    assert "--allowedTools" not in argv
    assert "--settings" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert not result.get("env_error")


def test_local_runner_launch_flips_process_finished(tmp_path):
    """The real LocalRunner.launch shell wrapper, end to end: it starts the harness detached
    and writes exit_code when the process ends. process_finished() is False while the process
    runs (pid alive, no exit_code yet) and True once the wrapper has written exit_code. Uses a
    trivial `cat` harness that echoes the brief — no model, no tokens (the whole suite otherwise
    runs the in-process runner, so this is the only coverage of the real launch mechanics)."""
    from garden.harness import Harness
    from garden.runs import Run

    # A "harness" that sleeps briefly (long enough to observe the running state) then echoes
    # its stdin (the brief) to stdout, so the wrapper's redirects and exit_code are exercised.
    h = Harness("tiny", {"command": ["sh", "-c", "sleep 0.5; cat"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    d = tmp_path / "run"
    d.mkdir()
    run = Run(task_id="T-001", run_id="r1", dir=str(d), runner="local")
    brief = tmp_path / "brief.md"
    brief.write_text("hello from the brief\n")

    runner.launch(run, tmp_path, brief, dict(os.environ))
    assert run.pid is not None and run.harness == "tiny"
    assert not run.process_finished()  # still sleeping: pid alive, exit_code not written yet

    try:
        os.waitpid(run.pid, 0)  # wait for the detached wrapper to finish (no sleep, no timeout)
    except ChildProcessError:
        pass
    assert run.process_finished()
    assert (d / "exit_code").read_text().strip() == "0"
    assert "hello from the brief" in (d / "stdout.json").read_text()


def test_ssh_runner_uses_bare_bin(sched, fake_github):
    t = sched.store.task("DM-001")
    t.runner = "ssh"
    sched.store.save(t)
    with patch("shutil.which", return_value="/resolved/claude"):
        sched.tick()
    run = sched.runs.latest("DM-001")
    remote_sh = (run.path / "remote.sh").read_text()
    # SSH runner must not resolve the binary path: the remote host may have it elsewhere
    assert "/resolved/claude" not in remote_sh


def test_ssh_runner_sets_garden_root(sched, fake_github):
    """The ssh remote script must export GARDEN_ROOT at a non-garden path, so a worker on a
    remote clone that is itself a garden cannot run garden commands against it."""
    t = sched.store.task("DM-001")
    t.runner = "ssh"
    sched.store.save(t)
    sched.tick()
    run = sched.runs.latest("DM-001")
    remote_sh = (run.path / "remote.sh").read_text()
    assert 'GARDEN_ROOT="$WT/.garden-no-live-garden"' in remote_sh


def test_ssh_remote_worker_runs_in_scrubbed_env(sched, garden, fake_github, tmp_path, monkeypatch):
    """The ssh runner's remote script must run the harness under the same allowlist as the
    local worker (runner.base.PASS_ENV plus worker_env.pass and setup.env): a host's ambient
    tokens (a GitHub token, cloud credentials, an ssh agent) must not reach the worker, while
    the harness's own key, the locale and the run identity survive."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    dump = tmp_path / "worker-env.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_DUMP", str(dump))
    t = sched.store.task("DM-001")
    t.runner = "ssh"
    sched.store.save(t)
    sched.tick()
    run = sched.runs.latest("DM-001")
    _wait_for_child(run)
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "CLAUDECODE"):
        assert name not in seen, f"{name} leaked into the remote worker"
    assert seen["ANTHROPIC_API_KEY"] == "sk-ant"  # the claude harness's own key (ANTHROPIC_*) survives
    assert seen["LC_ALL"] == "C.UTF-8"  # allowlisted locale survives
    assert seen["GARDEN_TASK_ID"] == "DM-001" and seen["GARDEN_RUN_ID"] == run.run_id
    assert seen["GARDEN_ROOT"].endswith(".garden-no-live-garden")
    # HOME is an isolated scratch home, not the remote login's, so the worker cannot read the
    # host's gh token, git credentials or ssh keys out of ~.
    assert seen["HOME"].endswith(".garden-home-DM-001") and seen["HOME"] != os.environ.get("HOME")
    # Harness homes are rebuilt under the scratch HOME, not passed through from the host.
    assert Path(seen["CLAUDE_CONFIG_DIR"]).parent == Path(seen["HOME"])
    assert Path(seen["CODEX_HOME"]).parent == Path(seen["HOME"])


def test_ssh_remote_worker_honours_config_dirs_override(sched, garden, fake_github, tmp_path, monkeypatch):
    """CG-218: `worker_env.config_dirs` overrides the remote script's CLAUDE_CONFIG_DIR/
    CODEX_HOME defaults, the same way it overrides `scrubbed_env` for the local runner."""
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg.setdefault("worker_env", {})["config_dirs"] = {"CLAUDE_CONFIG_DIR": "/srv/claude-creds"}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    from garden.scheduler import Scheduler
    from garden.store import Store

    store = Store(garden)
    sc = Scheduler(store, github=fake_github, log=print)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    dump = tmp_path / "worker-env.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_DUMP", str(dump))
    t = sc.store.task("DM-001")
    t.runner = "ssh"
    sc.store.save(t)
    sc.tick()
    run = sc.runs.latest("DM-001")
    _wait_for_child(run)
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert Path(seen["CLAUDE_CONFIG_DIR"]).parent == Path(seen["HOME"])
    assert Path(seen["CODEX_HOME"]).parent == Path(seen["HOME"])


def test_ssh_host_capacity(sched):
    for tid in ("DM-001", "DM-002"):
        t = sched.store.task(tid)
        t.runner = "ssh"
        t.depends_on = []
        sched.store.save(t)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # boxA max_parallel 1
    assert any("max_parallel" in e for e in rep.errors)


def test_stream_json_harness_end_to_end(garden, fake_github, monkeypatch):
    """output_format: stream-json produces JSONL stdout and is correctly reaped."""
    from garden.scheduler import Scheduler
    from garden.store import Store

    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["harnesses"]["claude"]["output_format"] = "stream-json"
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    sc = Scheduler(store, github=fake_github, log=print)
    sc.tick()
    run = sc.runs.latest("DM-001")
    assert run is not None
    evs = run.stdout_events()
    assert any(
        ev.get("type") == "assistant"
        and any(p.get("type") == "tool_use" for p in (ev.get("message") or {}).get("content") or [])
        for ev in evs
    )
    assert any(ev.get("type") == "result" for ev in evs)
    sc.tick()
    sc.store.invalidate()
    assert sc.store.task("DM-001").status == Status.IN_REVIEW
    run = sc.runs.latest("DM-001")
    assert run.result.get("status") == "done"
    assert run.usage.get("input_tokens") == 1234


def test_codex_planning_review_and_resume(sched, monkeypatch, fake_github):
    from garden.planner import import_plan, parse_plan, plan_prompt, run_planner

    sched.cfg.data["harness"] = "codex"
    sched.cfg.data["review"]["enabled"] = True
    sched.cfg.data["worker_env"]["pass"].append("FAKE_CODEX_*")
    (sched.store.root / "garden.yaml").write_text(yaml.safe_dump(sched.cfg.data))
    prompt = plan_prompt(sched.store, "demo", "p1")
    planned = parse_plan(run_planner(sched.store, prompt))
    tasks = import_plan(sched.store, "demo", "p1", planned, status="draft")
    assert tasks[0].title == "Codex planned task"
    monkeypatch.setenv("FAKE_CODEX_MODE", "needs_input")
    sched.tick()
    sched.tick()
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.status == Status.WAITING_HUMAN
    assert sched.state.get(task.id)["session_id"] == "th_1"
    run = sched.answer(task, "Use SQLite")
    assert run.mode == "resume" and run.session_id == "th_1"
    sched.tick()
    sched.tick()
    assert fake_github.created[0]["title"] == "Codex PR"
    assert any("checked" in c for c in fake_github.comments)
    assert "Use SQLite" in (sched.worktree_for(task) / "codex-resumed.txt").read_text()


def test_manual_runner_collects_cost_from_finish(garden):
    """CG-158: `garden finish --cost` records what a manual round cost, the same field an
    automated run reports from its harness usage, so a hand-worked task counts toward cost
    metrics instead of always showing as free."""
    from garden.runs import RunStore

    rs = RunStore(garden / ".garden")
    run = rs.new_run("DM-001", "manual", "work")
    ManualRunner.finish(run, {"status": "done", "summary": "by hand", "cost_usd": 3.5})

    collected = ManualRunner({}).collect(run)
    assert collected["cost_usd"] == 3.5
    assert collected["result"]["status"] == "done"


def test_manual_runner_collect_with_no_cost_reported(garden):
    from garden.runs import RunStore

    rs = RunStore(garden / ".garden")
    run = rs.new_run("DM-001", "manual", "work")
    ManualRunner.finish(run, {"status": "done", "summary": "by hand"})

    collected = ManualRunner({}).collect(run)
    assert collected["cost_usd"] is None
