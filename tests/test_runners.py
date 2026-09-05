import os
import subprocess
from unittest.mock import patch

import yaml

from garden.model import Status
from garden.runner.local import LocalRunner


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
    assert run.usage["input_tokens"] == 500 and run.result["summary"] == "codex did it with gpt-max"
    assert fake_github.created[0]["title"] == "Codex PR"


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


def test_ssh_runner_end_to_end(sched, garden, fake_github, tmp_path):
    t = sched.store.task("DM-001")
    t.runner = "ssh"
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
    assert (sched.worktree_for(t) / "worker-output.txt").exists()
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
