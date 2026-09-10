import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from garden.model import Status
from garden.proctree import descendants, pid_alive, process_snapshot
from garden.runner.local import LocalRunner
from garden.runner.manual import ManualRunner
from garden.runs import Run

_EXECUTION_LEASE_ENV = frozenset({
    "GARDEN_EXECUTION_RUN_DIR",
    "GARDEN_EXECUTION_OWNER",
    "GARDEN_HEAVY_EXECUTION",
    "GARDEN_OWNER_SCOPED",
    "GARDEN_EXECUTION_TIMEOUT_SECONDS",
    "GARDEN_VALIDATION_INHERITS_LEASE",
})


def _synthetic_child_env(
    env: dict[str, str] | None = None, **updates: str,
) -> dict[str, str]:
    """Build a child fixture env without borrowing the enclosing validation identity."""
    child = dict(os.environ if env is None else env)
    for key in _EXECUTION_LEASE_ENV:
        child.pop(key, None)
    child.update(updates)
    return child


def _local_process_snapshot(pgid: int) -> list[dict[str, object]]:
    """Describe the owned local-run group for a bounded-test failure message."""
    owned = {pgid, *descendants(pgid)}
    return [
        {"pid": process.pid, "ppid": process.ppid, "pgid": process.pgid,
         "state": process.state}
        for process in process_snapshot() if process.pid in owned
    ]


def _stop_and_reap_local_run(run: Run) -> None:
    """End a detached local fixture and reap its supervisor before returning."""
    if run.pid is None:
        return
    run.stop(timeout=1.0)
    try:
        os.waitpid(run.pid, 0)
    except ChildProcessError:
        pass


def _standalone_supervisor_env(**updates: str) -> dict[str, str]:
    """Build fixture env without inheriting the containing run's execution lease."""
    return _synthetic_child_env(**updates)


def test_synthetic_child_env_drops_enclosing_validation_lease(monkeypatch):
    """Runner fixtures receive a new lease instead of contending with pytest's owner."""
    inherited = {
        "GARDEN_EXECUTION_RUN_DIR": "/outer/run",
        "GARDEN_EXECUTION_OWNER": "outer-owner",
        "GARDEN_HEAVY_EXECUTION": "1",
        "GARDEN_OWNER_SCOPED": "1",
        "GARDEN_EXECUTION_TIMEOUT_SECONDS": "900",
        "GARDEN_VALIDATION_INHERITS_LEASE": "1",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UNRELATED_FIXTURE_VALUE", "kept")

    child = _synthetic_child_env()

    assert _EXECUTION_LEASE_ENV.isdisjoint(child)
    assert child["UNRELATED_FIXTURE_VALUE"] == "kept"


@contextmanager
def _launched_local_run(
    runner: LocalRunner,
    run: Run,
    worktree: Path,
    brief: Path,
    env: dict[str, str],
    *,
    cleanup: Callable[[], None] | None = None,
) -> Iterator[Run]:
    """Launch one real supervisor and always return its process ownership to pytest."""
    # These are disposable child-supervisor fixtures.  When pytest itself is running under
    # garden.validation it owns a validation lease; inheriting that identity would make the
    # synthetic child wait behind its own ancestor instead of exercising LocalRunner.
    runner.launch(run, worktree, brief, _synthetic_child_env(env))
    try:
        yield run
    finally:
        if cleanup is not None:
            cleanup()
        _stop_and_reap_local_run(run)


def _wait_for_local_run(run: Run, *, timeout: float = 3.0) -> None:
    """Wait for the supervisor's completion signal without an unbounded ``waitpid``.

    The supervisor remains the scheduler's process-group owner.  This test helper only
    reaps its direct child after ``process_finished`` has observed that group as gone,
    so it cannot consume a status while checking liveness.  Its finally block makes a
    future EOF regression fail promptly without leaving a sleeping fixture behind.
    """
    try:
        deadline = time.monotonic() + timeout
        while not run.process_finished() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.process_finished(), (
            f"LocalRunner did not finish within {timeout}s "
            f"(interpreter={sys.executable}, pid={run.pid}, "
            f"exit_code={run.read_exit_code()}, group={_local_process_snapshot(run.pid) if run.pid else []})"
        )
    finally:
        _stop_and_reap_local_run(run)


def _wait_for_local_harness_child(run, *, timeout: float = 1.0) -> None:
    """Confirm the detached supervisor launched its harness before the test continues."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if descendants(run.pid):
            return
        time.sleep(0.01)
    pytest.fail(
        f"local-run harness did not start "
        f"(interpreter={sys.executable}, pid={run.pid}, group={_local_process_snapshot(run.pid)})"
    )


def _private_adapter(tmp_path: Path, *, version: str = "1", capabilities: str = "{'detached': True, 'remote': False}") -> str:
    """Package a synthetic adapter outside garden, as an operator would."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    package_name = f"private_adapter_{abs(hash(tmp_path))}"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "runner.py").write_text(
        "from garden.runner.base import Runner\n"
        "class SyntheticRunner(Runner):\n"
        f"    adapter_version = {version}\n"
        f"    capabilities = {capabilities}\n"
        "    def start(self, run, worktree, brief_text): pass\n"
        "    def collect(self, run): return {}\n"
    )
    return f"{package_name}.runner.SyntheticRunner"


def test_private_runner_adapter_resolves_from_operator_configuration(tmp_path, monkeypatch):
    """A separately packaged adapter needs no change to the public garden package."""
    from garden.harness import Harness
    from garden.runner import get_runner

    monkeypatch.syspath_prepend(str(tmp_path))
    path = _private_adapter(tmp_path)
    runner = get_runner("synthetic", {"_runner_adapters": {"synthetic": {"path": path}}}, Harness("claude", {}))

    assert runner.name == "synthetic"
    assert runner.capabilities == {"detached": True, "remote": False}


def test_scheduler_routes_private_adapters_through_ordinary_runner_setup(sched, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    sched.cfg.data["runner_adapters"] = {"synthetic": {"path": _private_adapter(tmp_path)}}
    task = sched.store.task("DM-001")
    task.runner = "synthetic"

    runner = sched.runner_for(task)

    assert runner.name == "synthetic"
    assert runner.config["setup"] == sched.cfg.product_setup(task.product)
    assert runner.config["resources"] == sched.cfg.get("resources")


def test_private_local_adapter_obeys_selection_and_atomic_local_admission(sched, tmp_path, monkeypatch):
    """A configured alias cannot bypass the controller host's local capacity."""
    from garden.scheduler.report import TickReport
    from garden.scheduler.resources import ResourcePressureError

    monkeypatch.syspath_prepend(str(tmp_path))
    sched.cfg.data["runner_adapters"] = {"synthetic": {"path": _private_adapter(tmp_path)}}
    sched.cfg.data["resources"] = {"max_parallel": 1}
    task = sched.store.task("DM-001")
    task.runner = "synthetic"
    sched.store.save(task)
    occupied = sched.runs.new_run("DM-002", "local", mode="check")
    occupied.save()

    rep = TickReport()
    sched.dispatch_ready(rep)

    assert rep.dispatched == []
    assert sched.runs.runs_for(task.id) == []
    with pytest.raises(ResourcePressureError, match="waits for a local execution slot"):
        sched.dispatch(task)

    occupied.status = "done"
    occupied.save()
    run = sched.dispatch(task)
    assert run.runner == "synthetic"
    assert run.is_local_execution
    assert sched.local_slots_free() == 0


def test_private_runner_adapter_rejects_bad_contract_and_builtin_replacement(tmp_path, monkeypatch):
    from garden.runner import RunnerError, get_runner

    monkeypatch.syspath_prepend(str(tmp_path))
    path = _private_adapter(tmp_path, version="2")
    with pytest.raises(RunnerError, match="interface version 2; expected 1"):
        get_runner("synthetic", {"_runner_adapters": {"synthetic": {"path": path}}})
    bad_capabilities = _private_adapter(tmp_path / "bad-capabilities", capabilities="{}")
    monkeypatch.syspath_prepend(str(tmp_path / "bad-capabilities"))
    with pytest.raises(RunnerError, match="must declare capabilities"):
        get_runner("capability-test", {"_runner_adapters": {"capability-test": {"path": bad_capabilities}}})
    with pytest.raises(RunnerError, match="cannot replace built-in"):
        get_runner("local", {"_runner_adapters": {"local": {"path": path}}})


def test_private_runner_adapter_reports_missing_import_without_config_load_importing(tmp_path):
    from garden.config import Config
    from garden.runner import RunnerError, get_runner

    # An import can have arbitrary effects, so simply inspecting a garden must not resolve it.
    (tmp_path / "garden.yaml").write_text(
        "runner_adapters:\n  synthetic:\n    path: missing_adapter.Runner\n"
    )
    config = Config.load(tmp_path)
    assert config.runner_adapter("synthetic") == {"path": "missing_adapter.Runner"}
    with pytest.raises(RunnerError, match="could not import 'missing_adapter'"):
        get_runner("synthetic", {"_runner_adapters": config.get("runner_adapters")})


def test_runner_adapter_registrations_are_fenced_as_executable_configuration():
    from garden.config import executable_diff

    original = {"runner_adapters": {"synthetic": {"path": "private.adapter.Runner"}}}
    changed = {"runner_adapters": {"synthetic": {"path": "replacement.adapter.Runner"}}}

    assert executable_diff(original, changed) == ["runner_adapters"]


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
@pytest.mark.needs_remote_clone
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


@pytest.mark.needs_remote_clone
def test_ssh_dispatch_uses_an_alias_in_shared_evidence_and_keeps_target_local(garden, fake_github):
    """The connection target is needed by the local SSH wrapper, never shared context."""
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    target = "operator@host-203-0-113-10.internal"
    cfg["ssh"]["hosts"][0]["host"] = target
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden), github=fake_github, log=print)
    task = sched.store.task("DM-001")
    task.runner = "ssh"
    sched.store.save(task)
    sched.tick()
    run = sched.runs.latest("DM-001")

    assert run.host == "boxA"
    assert target not in (run.path / "brief.md").read_text()
    assert target not in (garden / "demo" / "p1" / "tasks" / "DM-001-first.md").read_text()
    assert target not in (garden / ".garden" / "events.jsonl").read_text()
    assert target in (run.path / "command.txt").read_text()  # ignored local diagnostic artifact


def test_local_runner_doctor_windows():
    with patch("os.name", "nt"):
        runner = LocalRunner({}, None)
        errors = runner.doctor()
    assert len(errors) == 1 and "WSL" in errors[0]


def test_local_runner_preserves_fractional_timeout_minutes(tmp_path):
    from garden.harness import Harness

    runner = LocalRunner({"timeout_minutes": 0.5}, Harness("tiny", {"command": ["true"]}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    brief = run_dir / "brief.md"
    brief.write_text("")
    run = Run(task_id="T-1", run_id="fractional", dir=str(run_dir), runner="local")

    runner.launch(run, tmp_path, brief, _synthetic_child_env(
        GARDEN_HEAVY_TEST_PARALLEL="1", XDG_RUNTIME_DIR=str(tmp_path),
    ))
    os.waitpid(run.pid, 0)

    assert run.read_exit_code() == 0
    execution = json.loads((run.path / "execution.json").read_text())
    assert execution["timeout_seconds"] == 30
    assert execution["timeout_kind"] == "worker"
    assert "timeout 30 " not in (run.path / "command.txt").read_text()


def test_local_runner_enforces_timeout_without_a_shell_timeout_utility(tmp_path):
    """The Python supervisor, not an optional GNU command, owns the worker deadline."""
    from garden.harness import Harness

    runner = LocalRunner({"timeout_minutes": 0.005}, Harness("tiny", {"command": ["sleep", "30"]}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    brief = run_dir / "brief.md"
    brief.write_text("")
    run = Run(task_id="T-1", run_id="bounded", dir=str(run_dir), runner="local")

    runner.launch(run, tmp_path, brief, _synthetic_child_env(
        GARDEN_HEAVY_TEST_PARALLEL="1", XDG_RUNTIME_DIR=str(tmp_path),
    ))
    _wait_for_local_run(run)

    assert run.read_exit_code() == 124
    timeout = json.loads((run.path / "execution_timeout.json").read_text())
    assert timeout["kind"] == "worker_execution_timeout"
    assert timeout["reason"] == "worker execution exceeded 0.3 seconds"


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

    with _launched_local_run(runner, run, tmp_path, brief, dict(os.environ)):
        assert run.pid is not None and run.harness == "tiny"
        assert not run.process_finished()  # still sleeping: pid alive, exit_code not written yet
        _wait_for_local_harness_child(run)
        _wait_for_local_run(run)
        assert (d / "exit_code").read_text().strip() == "0"
        assert "hello from the brief" in (d / "stdout.json").read_text()


def test_local_runner_reports_nonzero_after_stdin_eof(tmp_path):
    """The same supervised stdin path preserves a consumer's nonzero exit status."""
    from garden.harness import Harness
    from garden.runs import Run

    h = Harness("tiny", {"command": ["sh", "-c", "cat >/dev/null; exit 7"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    d = tmp_path / "run"
    d.mkdir()
    run = Run(task_id="T-001", run_id="r1", dir=str(d), runner="local")
    brief = tmp_path / "brief.md"
    brief.write_text("input that must reach EOF\n")

    with _launched_local_run(runner, run, tmp_path, brief, dict(os.environ)):
        _wait_for_local_run(run)
        assert run.read_exit_code() == 7


def test_local_runner_cancellation_stops_a_stdin_consumer(tmp_path):
    """Cancellation reaches a still-active consumer and leaves no owned process group."""
    from garden.harness import Harness
    from garden.runs import Run

    h = Harness("tiny", {"command": ["sh", "-c", "sleep 30; cat"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    d = tmp_path / "run"
    d.mkdir()
    run = Run(task_id="T-001", run_id="r1", dir=str(d), runner="local")
    brief = tmp_path / "brief.md"
    brief.write_text("input that must not keep a cancelled run alive\n")

    with _launched_local_run(runner, run, tmp_path, brief, dict(os.environ)):
        assert not run.process_finished()
        _wait_for_local_harness_child(run)
        assert run.stop(timeout=2.0)
        _wait_for_local_run(run)
        assert run.read_exit_code() in (-signal.SIGTERM, 143)


def test_local_runner_lifecycle_fixture_reaps_after_an_early_assertion(tmp_path):
    """An assertion before completion cannot strand the detached stdin consumer."""
    from garden.harness import Harness

    h = Harness("tiny", {"command": ["sh", "-c", "sleep 30; cat"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    d = tmp_path / "run"
    d.mkdir()
    run = Run(task_id="T-001", run_id="r1", dir=str(d), runner="local")
    brief = tmp_path / "brief.md"
    brief.write_text("input that must not outlive a failed assertion\n")

    with pytest.raises(AssertionError, match="intentional fixture failure"):
        with _launched_local_run(runner, run, tmp_path, brief, dict(os.environ)):
            _wait_for_local_harness_child(run)
            raise AssertionError("intentional fixture failure")

    assert run.process_finished()
    with pytest.raises(ChildProcessError):
        os.waitpid(run.pid, os.WNOHANG)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux subreaper semantics")
def test_local_runner_owns_daemonized_descendants_until_they_exit(tmp_path):
    """A child in a new session still keeps its run active through the subreaper."""
    from garden.harness import Harness
    from garden.runs import Run

    h = Harness("tiny", {"command": ["sh", "-c", "setsid sh -c 'sleep 0.8' >/dev/null 2>&1 &"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    d = tmp_path / "run"
    d.mkdir()
    run = Run(task_id="T-001", run_id="r1", dir=str(d), runner="local")
    brief = tmp_path / "brief.md"
    brief.write_text("")

    with _launched_local_run(runner, run, tmp_path, brief, dict(os.environ)):
        assert run.pid is not None
        # The harness shell returns immediately, but the supervisor remains the subreaper for
        # its new-session descendant and withholds the completion signal.
        deadline = time.monotonic() + 0.5
        while not (d / "stdout.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not run.process_finished()
        _wait_for_local_run(run)
        isolation = __import__("json").loads((d / "isolation.json").read_text())
        assert isolation == {"configured": False, "enforced": False,
                             "reason": "execution cgroup is not configured"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux subreaper semantics")
def test_local_supervisor_reaps_adopted_exits_while_leader_is_alive(tmp_path):
    """Exited orphaned grandchildren do not block their still-live run leader."""
    from garden.harness import Harness
    from garden.runs import Run

    orphan = tmp_path / "orphan.py"
    orphan.write_text(
        "import os, pathlib\n"
        "pid_file = pathlib.Path(os.environ['ORPHAN_PIDS'])\n"
        "for _ in range(3):\n"
        " pid = os.fork()\n"
        " if pid == 0:\n"
        "  with pid_file.open('a') as out: out.write(f'{os.getpid()}\\n')\n"
        "  os._exit(0)\n"
        "os._exit(0)\n"
    )
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "subprocess.run([sys.executable, os.environ['ORPHAN_SCRIPT']], check=True)\n"
        "release = pathlib.Path(os.environ['LEADER_RELEASE'])\n"
        "while not release.exists(): time.sleep(0.01)\n"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    brief = run_dir / "brief.md"
    brief.write_text("")
    run = Run(task_id="T-1", run_id="reap", dir=str(run_dir), runner="local")
    runner = LocalRunner({"timeout_minutes": 1}, Harness("orphan", {"command": [sys.executable, str(leader)]}))
    pids = tmp_path / "orphan-pids"
    release = tmp_path / "release"
    with _launched_local_run(
        runner, run, tmp_path, brief,
        _standalone_supervisor_env(
            ORPHAN_SCRIPT=str(orphan), ORPHAN_PIDS=str(pids), LEADER_RELEASE=str(release)
        ),
        cleanup=release.touch,
    ):
        deadline = time.monotonic() + 3
        while (not pids.exists() or len(pids.read_text().splitlines()) < 3) and time.monotonic() < deadline:
            time.sleep(0.01)
        orphan_pids = pids.read_text().splitlines()
        assert len(orphan_pids) == 3
        while any(Path(f"/proc/{pid}").exists() for pid in orphan_pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(Path(f"/proc/{pid}").exists() for pid in orphan_pids)
        assert not run.process_finished()  # the primary leader and its run lease remain live

        release.touch()
        _wait_for_local_run(run)
        assert run.read_exit_code() == 0


def test_local_supervisor_preserves_nonzero_status_until_descendants_exit(tmp_path):
    """The leader's real status is retained while completion waits for live work."""
    run_dir = tmp_path / "nonzero"
    run_dir.mkdir()
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "garden.run_supervisor", str(run_dir), "sleep 0.25 & exit 7"],
        check=False,
        env=_standalone_supervisor_env(),
    )

    assert result.returncode == 7
    assert (run_dir / "exit_code").read_text() == "7"
    assert time.monotonic() - started >= 0.2


def test_local_supervisors_share_heavy_budget_and_recover_after_exit(tmp_path):
    """Independent launchers queue on the host lease; exit releases it without cleanup."""
    from garden.harness import Harness
    from garden.runs import Run

    h = Harness("tiny", {"command": ["sh", "-c", "sleep 0.35"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    runs = []
    for number in (1, 2):
        d = tmp_path / f"run{number}"
        d.mkdir()
        brief = d / "brief.md"
        brief.write_text("")
        run = Run(task_id=f"T-{number}", run_id=f"r{number}", dir=str(d), runner="local")
        env = _synthetic_child_env(GARDEN_HEAVY_TEST_PARALLEL="1",
                                   XDG_RUNTIME_DIR=str(tmp_path), GARDEN_HEAVY_EXECUTION="1")
        runner.launch(run, tmp_path, brief, env)
        runs.append(run)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        states = [(r.path / "execution.json").read_text() for r in runs if (r.path / "execution.json").exists()]
        if any('"state": "running"' in s for s in states) and any('"state": "waiting"' in s for s in states):
            break
        time.sleep(0.01)
    assert sum('"state": "running"' in (r.path / "execution.json").read_text() for r in runs) == 1
    assert sum('"state": "waiting"' in (r.path / "execution.json").read_text() for r in runs) == 1
    for run in runs:
        os.waitpid(run.pid, 0)
    assert all(run.process_finished() for run in runs)


def test_conflicting_garden_limits_keep_first_authoritative_capacity(tmp_path):
    """A limit-2 garden cannot add a slot while the shared authority is limit 1."""
    from garden.harness import Harness
    from garden.runs import Run

    runner = LocalRunner({"timeout_minutes": 0}, Harness("tiny", {"command": ["sh", "-c", "sleep 0.8"]}))
    runs = []
    for number, limit in ((1, 1), (2, 2)):
        run_dir = tmp_path / f"mixed-{number}"
        run_dir.mkdir()
        brief = run_dir / "brief.md"
        brief.write_text("")
        run = Run(task_id=f"T-{number}", run_id=f"mixed-{number}", dir=str(run_dir), runner="local")
        runner.launch(run, tmp_path, brief, _synthetic_child_env(
            XDG_RUNTIME_DIR=str(tmp_path), GARDEN_HEAVY_TEST_PARALLEL=str(limit),
            GARDEN_HEAVY_EXECUTION="1",
        ))
        runs.append(run)
        if number == 1:
            deadline = time.monotonic() + 2
            while not (run_dir / "execution.json").exists() and time.monotonic() < deadline:
                time.sleep(0.01)

    deadline = time.monotonic() + 2
    conflict = None
    while time.monotonic() < deadline:
        path = runs[1].path / "execution.json"
        if path.exists() and (conflict := json.loads(path.read_text())).get("conflict"):
            break
        time.sleep(0.01)
    assert conflict is not None
    assert conflict["state"] == "waiting"
    assert conflict["limit"] == 1 and conflict["requested_limit"] == 2
    assert "conflicts with authoritative limit 1" in conflict["reason"]
    for run in runs:
        os.waitpid(run.pid, 0)
        assert run.read_exit_code() == 0


def test_model_sessions_overlap_while_their_heavy_validations_serialize(tmp_path):
    """Agent capacity is independent of the authoritative local validation budget."""
    from garden.harness import Harness
    from garden.runs import Run

    counter = tmp_path / "counter.py"
    counter.write_text(
        "import fcntl, pathlib, sys, time\n"
        "name, delay = sys.argv[1], float(sys.argv[2])\n"
        "state = pathlib.Path(name + '.txt')\n"
        "with pathlib.Path(name + '.lock').open('a+') as lock:\n"
        " fcntl.flock(lock, fcntl.LOCK_EX)\n"
        " active, peak = map(int, (state.read_text() if state.exists() else '0 0').split())\n"
        " state.write_text(f'{active + 1} {max(active + 1, peak)}')\n"
        "time.sleep(delay)\n"
        "with pathlib.Path(name + '.lock').open('a+') as lock:\n"
        " fcntl.flock(lock, fcntl.LOCK_EX)\n"
        " active, peak = map(int, state.read_text().split())\n"
        " state.write_text(f'{active - 1} {peak}')\n"
    )
    target = tmp_path / "test_heavy.py"
    target.write_text(
        "import subprocess, sys\n\n"
        "def test_heavy():\n"
        f" subprocess.run([sys.executable, {str(counter)!r}, 'heavy', '0.25'], check=True)\n"
    )
    validation = (
        '"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- '
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(str(target))} -q"
    )
    command = ["sh", "-c", f"{shlex.quote(sys.executable)} {counter} model 0.15 & {validation}; wait"]
    runner = LocalRunner({"timeout_minutes": 1}, Harness("agent", {"command": command}))
    runs = []
    for number in (1, 2):
        run_dir = tmp_path / f"agent-{number}"
        run_dir.mkdir()
        brief = run_dir / "brief.md"
        brief.write_text("")
        run = Run(task_id=f"T-{number}", run_id=f"agent-{number}", dir=str(run_dir), runner="local")
        runner.launch(run, tmp_path, brief, _synthetic_child_env(
            XDG_RUNTIME_DIR=str(tmp_path), GARDEN_HEAVY_TEST_PARALLEL="1",
        ))
        runs.append(run)
    for run in runs:
        os.waitpid(run.pid, 0)
        assert run.read_exit_code() == 0
    assert (tmp_path / "model.txt").read_text() == "0 2"
    assert (tmp_path / "heavy.txt").read_text() == "0 1"


def test_two_supported_pytest_launches_share_one_real_workload_slot(tmp_path):
    """A real focused pytest target runs once while the other supported launch waits."""
    from garden.harness import Harness
    from garden.runs import Run

    release = tmp_path / "release-pytest"
    target = tmp_path / "test_bounded_target.py"
    target.write_text(
        "import os, time\nfrom pathlib import Path\n\n"
        "def test_bounded_workload():\n"
        "    release = Path(os.environ['TEST_RELEASE'])\n"
        "    deadline = time.monotonic() + 5\n"
        "    while not release.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    assert release.exists()\n"
    )
    command = [sys.executable, "-m", "pytest", str(target), "-q"]
    runner = LocalRunner({"timeout_minutes": 1}, Harness("focused-pytest", {"command": command}))
    runs = []
    for number in (1, 2):
        run_dir = tmp_path / f"pytest-run-{number}"
        run_dir.mkdir()
        brief = run_dir / "brief.md"
        brief.write_text("")
        run = Run(task_id=f"T-{number}", run_id=f"pytest-{number}", dir=str(run_dir), runner="local")
        runner.launch(run, tmp_path, brief, _synthetic_child_env(
            GARDEN_HEAVY_TEST_PARALLEL="1", XDG_RUNTIME_DIR=str(tmp_path),
            GARDEN_HEAVY_EXECUTION="1", GARDEN_EXECUTION_CGROUP="", TEST_RELEASE=str(release),
        ))
        runs.append(run)

    deadline = time.monotonic() + 3
    observed = set()
    while time.monotonic() < deadline:
        for run in runs:
            if (run.path / "execution.json").exists():
                observed.add(json.loads((run.path / "execution.json").read_text())["state"])
        if observed == {"running", "waiting"}:
            break
        time.sleep(0.01)
    release.touch()
    assert observed == {"running", "waiting"}
    running = next(run for run in runs
                   if json.loads((run.path / "execution.json").read_text())["state"] == "running")
    execution = json.loads((running.path / "execution.json").read_text())
    assert execution["pid"] == running.pid
    assert "pytest" in (running.path / "command.txt").read_text()
    for run in runs:
        os.waitpid(run.pid, 0)
        assert run.read_exit_code() == 0
        assert "1 passed" in (run.path / "stdout.json").read_text()


def test_local_worker_env_carries_execution_budget(tmp_path):
    from garden.harness import Harness
    from garden.runs import Run

    runner = LocalRunner({"resources": {"heavy_test_parallel": 3,
                                        "execution_cgroup": "/sys/fs/cgroup/example"},
                          "checks": {"timeout_seconds": 731}},
                         Harness("tiny", {"command": ["true"]}))
    run = Run(task_id="T-1", run_id="r1", dir=str(tmp_path / "run"), runner="local")
    env = runner.worker_env(run, {}, tmp_path)
    assert env["GARDEN_HEAVY_TEST_PARALLEL"] == "3"
    assert env["GARDEN_EXECUTION_CGROUP"] == "/sys/fs/cgroup/example"
    assert env["GARDEN_VALIDATION_TIMEOUT_SECONDS"] == "731"
    assert "GARDEN_EXECUTION_TIMEOUT_SECONDS" not in env


def test_openrouter_key_is_added_only_to_harness_environment(tmp_path, monkeypatch):
    from garden.harness import Harness
    from garden.runs import Run

    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-secret")
    runner = LocalRunner({}, Harness("openrouter", {}))
    run = Run(task_id="T-1", run_id="r1", dir=str(tmp_path / "run"), runner="local")
    worker = runner.worker_env(run, {}, tmp_path)
    assert "OPENROUTER_API_KEY" not in worker
    assert runner.harness_environment(worker)["OPENROUTER_API_KEY"] == "provider-secret"


def test_ordinary_pytest_deadlines_are_configured(request):
    assert float(request.config.getini("timeout")) == 120.0
    assert request.config.getini("timeout_method") == "signal"
    assert float(request.config.getini("session_timeout")) == 900.0


def _supervisor_test_env(tmp_path: Path, *, timeout: float | None = None) -> dict[str, str]:
    control = {
        "GARDEN_EXECUTION_RUN_DIR", "GARDEN_EXECUTION_OWNER", "GARDEN_OWNER_SCOPED",
        "GARDEN_EXECUTION_TIMEOUT_SECONDS",
    }
    env = {key: value for key, value in os.environ.items() if key not in control}
    env.update({
        "GARDEN_HEAVY_EXECUTION": "1",
        "GARDEN_HEAVY_TEST_PARALLEL": "1",
        "GARDEN_EXECUTION_CGROUP": "",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    })
    if timeout is not None:
        env["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = str(timeout)
    return env


def _supervisor_command(run_dir: Path, command: list[str]) -> list[str]:
    return [sys.executable, "-m", "garden.run_supervisor", str(run_dir), shlex.join(command)]


def test_validation_execution_timeout_kills_adopted_child_and_releases_slot(tmp_path):
    """The hard clock covers descendant drain and never terminates the calling process."""
    child_pid = tmp_path / "child.pid"
    progress = tmp_path / "progress.log"
    spawner = tmp_path / "spawn_child.py"
    spawner.write_text(
        "import os, pathlib, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        f" pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        f" progress = pathlib.Path({str(progress)!r})\n"
        " while True:\n"
        "  with progress.open('a') as output: output.write('still running\\n')\n"
        "  time.sleep(0.03)\n"
        " os._exit(0)\n"
        "while True: time.sleep(1)\n"
    )
    run_dir = tmp_path / "timed"
    run_dir.mkdir()

    started = time.monotonic()
    result = subprocess.run(
        _supervisor_command(run_dir, [sys.executable, str(spawner)]),
        env=_supervisor_test_env(tmp_path, timeout=0.25),
        capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 2
    assert (run_dir / "exit_code").read_text() == "124"
    timeout = json.loads((run_dir / "validation_timeout.json").read_text())
    execution = json.loads((run_dir / "execution.json").read_text())
    assert timeout["kind"] == "validation_execution_timeout"
    assert timeout["timeout_seconds"] == 0.25 and timeout["exit_code"] == 124
    assert execution["state"] == "timeout" and execution["reason"] == timeout["reason"]
    assert child_pid.exists() and not pid_alive(int(child_pid.read_text()))
    assert progress.read_text().count("still running") >= 2  # output never resets the fixed clock

    # The parent pytest/model process is still executing, and the kernel lock was released.
    followup = tmp_path / "followup"
    followup.mkdir()
    released = subprocess.run(
        _supervisor_command(followup, ["true"]), env=_supervisor_test_env(tmp_path, timeout=1),
        capture_output=True, text=True, timeout=3,
    )
    assert released.returncode == 0
    assert json.loads((followup / "execution.json").read_text())["state"] == "finished"


def test_validation_execution_clock_starts_after_admission_wait(tmp_path):
    """Waiting for the shared slot is observable time, not execution timeout consumption."""
    release = tmp_path / "release"
    holder_script = tmp_path / "holder.py"
    holder_script.write_text(
        "import pathlib, sys, time\n"
        "release = pathlib.Path(sys.argv[1])\n"
        "while not release.exists(): time.sleep(0.01)\n"
    )
    holder_dir = tmp_path / "holder"
    waiting_dir = tmp_path / "waiting"
    holder_dir.mkdir()
    waiting_dir.mkdir()
    holder = subprocess.Popen(
        _supervisor_command(holder_dir, [sys.executable, str(holder_script), str(release)]),
        env=_supervisor_test_env(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    waiting = None
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (holder_dir / "execution.json").exists() and json.loads(
                (holder_dir / "execution.json").read_text()
            ).get("state") == "running":
                break
            time.sleep(0.01)
        assert json.loads((holder_dir / "execution.json").read_text())["state"] == "running"

        waiting = subprocess.Popen(
            _supervisor_command(waiting_dir, ["sleep", "30"]),
            env=_supervisor_test_env(tmp_path, timeout=0.2),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (waiting_dir / "execution.json").exists() and json.loads(
                (waiting_dir / "execution.json").read_text()
            ).get("state") == "waiting":
                break
            time.sleep(0.01)
        queued = json.loads((waiting_dir / "execution.json").read_text())
        assert queued["state"] == "waiting" and queued["waiting_since"]
        time.sleep(0.3)  # longer than the execution budget, while admission remains blocked
        assert waiting.poll() is None
        assert not (waiting_dir / "validation_timeout.json").exists()

        release.touch()
        assert holder.wait(timeout=2) == 0
        assert waiting.wait(timeout=3) == 124
        timed = json.loads((waiting_dir / "execution.json").read_text())
        assert timed["state"] == "timeout"
        assert timed["admission_wait_started_at"] == queued["waiting_since"]
        assert timed["execution_started_at"] >= timed["admitted_at"]
    finally:
        release.touch()
        for process in (holder, waiting):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)


def test_execution_cgroup_requires_finite_limits_and_verified_migration(tmp_path, monkeypatch):
    import garden.run_supervisor as supervisor

    group = tmp_path / "bounded"
    group.mkdir()
    for name, value in (("cgroup.procs", ""), ("cpu.max", "100000 100000"),
                        ("memory.high", "2147483648"), ("memory.max", "2684354560")):
        (group / name).write_text(value)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("GARDEN_EXECUTION_CGROUP", str(group))

    monkeypatch.setattr(supervisor, "_process_cgroup_path", lambda: group.resolve())
    supervisor._enter_execution_cgroup(run_dir)
    status = json.loads((run_dir / "isolation.json").read_text())
    assert status["enforced"] is True
    assert status["limits"]["cpu.max"] == "100000 100000"

    monkeypatch.setattr(supervisor, "_process_cgroup_path", lambda: tmp_path.resolve())
    supervisor._enter_execution_cgroup(run_dir)
    status = json.loads((run_dir / "isolation.json").read_text())
    assert status["enforced"] is False
    assert "migration failed" in status["reason"]


def test_nested_supported_launch_takes_owner_scoped_lease(tmp_path, monkeypatch):
    import garden.run_supervisor as supervisor

    run_dir = tmp_path / "nested"
    run_dir.mkdir()
    # This directly acquires a synthetic host slot, so keep it out of an enclosing
    # garden.validation supervisor's real per-user lock namespace.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("GARDEN_EXECUTION_OWNER", "outer-run")
    monkeypatch.setenv("GARDEN_HEAVY_TEST_PARALLEL", "1")
    slot = supervisor._execution_slot(run_dir, lambda: False, owner_scoped=True)
    status = json.loads((run_dir / "execution.json").read_text())
    assert slot is not None
    assert status["state"] == "running" and status["owner_scoped"] is True


def test_validation_wrapper_applies_configured_execution_timeout(tmp_path, monkeypatch):
    import garden.validation as validation

    outer = tmp_path / "outer"
    outer.mkdir()
    monkeypatch.setenv("GARDEN_EXECUTION_RUN_DIR", str(outer))
    monkeypatch.setenv("GARDEN_EXECUTION_OWNER", "owned-run")
    monkeypatch.setenv("GARDEN_VALIDATION_TIMEOUT_SECONDS", "731")
    monkeypatch.delenv("GARDEN_HEAVY_EXECUTION", raising=False)
    monkeypatch.delenv("GARDEN_OWNER_SCOPED", raising=False)
    monkeypatch.delenv("GARDEN_VALIDATION_INHERITS_LEASE", raising=False)
    monkeypatch.setattr(sys, "argv", ["garden.validation", "--", "pytest", "-q"])
    monkeypatch.setattr(validation, "_source_state", lambda _cwd: ("head", ""))
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, env=dict(os.environ), kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert validation.main() == 0

    assert captured["argv"][:3] == [sys.executable, "-m", "garden.run_supervisor"]
    assert captured["argv"][-1].startswith("pytest -q --deselect=")
    assert captured["env"]["GARDEN_OWNER_SCOPED"] == "1"
    assert captured["env"]["GARDEN_EXECUTION_TIMEOUT_SECONDS"] == "731"

    monkeypatch.setenv("GARDEN_VALIDATION_TIMEOUT_SECONDS", "9999")
    assert validation.bounded_validation_timeout_seconds() == 900


def test_nested_validation_inherits_an_enclosing_validation_lease(tmp_path):
    """A full suite can run a supported validation without waiting on itself."""
    outer = tmp_path / "outer"
    outer.mkdir()
    inner_status = tmp_path / "inner-status.json"
    inner_test = tmp_path / "test_inner.py"
    inner_test.write_text(
        "import json\n"
        "from pathlib import Path\n\n"
        "def test_inner():\n"
        f"    Path({str(inner_status)!r}).write_text(json.dumps({{'ok': True}}))\n"
    )
    outer_test = tmp_path / "test_outer.py"
    outer_test.write_text(
        "import subprocess, sys\n\n"
        "def test_outer():\n"
        "    subprocess.run([sys.executable, '-m', 'garden.validation', '--', "
        f"sys.executable, '-m', 'pytest', {str(inner_test)!r}, '-q'], check=True)\n"
    )
    env = _supervisor_test_env(tmp_path)
    env.pop("GARDEN_HEAVY_EXECUTION")
    result = subprocess.run(
        _supervisor_command(
            outer,
            [sys.executable, "-m", "garden.validation", "--",
             sys.executable, "-m", "pytest", str(outer_test), "-q"],
        ),
        env=env, capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(inner_status.read_text()) == {"ok": True}
    statuses = list((outer / "validations").glob("*/execution.json"))
    assert len(statuses) == 2
    assert any(json.loads(path.read_text()).get("inherited_lease") is True for path in statuses)


def test_runtime_leases_use_private_fallback_and_reject_hostile_files(tmp_path, monkeypatch):
    import garden.run_supervisor as supervisor

    fallback = tmp_path / "tmp"
    fallback.mkdir()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    class StickyTmp:
        def lstat(self):
            return type("TmpStat", (), {"st_mode": stat.S_IFDIR | 0o1777, "st_uid": 0})()

        def is_dir(self):
            return True

        def is_symlink(self):
            return False

        def __truediv__(self, child):
            return fallback / child

    monkeypatch.setattr(supervisor, "Path", lambda value: StickyTmp() if value == "/tmp" else Path(value))
    root = fallback / f"garden-{os.getuid()}"
    root.symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError, match="private runtime directory"):
        supervisor._private_runtime_dir()
    root.unlink()
    root = supervisor._private_runtime_dir()
    assert root.name == f"garden-{os.getuid()}"
    assert root.stat().st_mode & 0o777 == 0o700

    hostile = root / f"garden-heavy-test-{os.getuid()}-capacity.json"
    hostile.symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError, match="unsafe runtime file"):
        supervisor._authoritative_limit(1)


def test_runtime_leases_accept_the_system_tmp_directory(monkeypatch):
    """Linux's /tmp directory and Darwin's root-owned /tmp symlink are both safe fallbacks."""
    import garden.run_supervisor as supervisor

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    root = supervisor._private_runtime_dir()

    assert root.parent == Path("/tmp").resolve()
    assert root.name == f"garden-{os.getuid()}"
    assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", [
    "garden-heavy-test-{uid}-capacity.json",
    "garden-heavy-test-{uid}-capacity.lock",
    "garden-heavy-test-{uid}-0.lock",
    "garden-heavy-test-{uid}-owner-owner.lock",
])
@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFDIR])
def test_safe_runtime_file_rejects_foreign_and_nonregular_fstat_results(tmp_path, monkeypatch, name, mode):
    """Every metadata and lease-file name fails closed on an unsafe fstat result."""
    import garden.run_supervisor as supervisor

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = supervisor._private_runtime_dir()
    expected_name = name.format(uid=os.getuid())
    real_fstat = supervisor.os.fstat

    def answering_for_the_expected_file(**unsafe):
        """An `fstat` that reports *unsafe* for the descriptor open on the expected file.

        The descriptor is recognised by the inode it is open on, rather than by a
        `/proc/self/fd` readlink, so the substitution works wherever the suite runs.
        """
        def fstat(fd):
            truth = real_fstat(fd)
            try:
                target = os.stat(root / expected_name)
            except OSError:
                return truth
            if (truth.st_dev, truth.st_ino) != (target.st_dev, target.st_ino):
                return truth
            return type("UnsafeStat", (), unsafe)()
        return fstat

    monkeypatch.setattr(
        supervisor.os,
        "fstat",
        answering_for_the_expected_file(st_uid=os.getuid() + 1, st_mode=stat.S_IFREG | 0o600),
    )
    with pytest.raises(RuntimeError, match="not a user-owned regular file"):
        supervisor._safe_runtime_file(root, expected_name)

    monkeypatch.setattr(
        supervisor.os,
        "fstat",
        answering_for_the_expected_file(st_uid=os.getuid(), st_mode=mode | 0o600),
    )
    with pytest.raises(RuntimeError, match="not a user-owned regular file"):
        supervisor._safe_runtime_file(root, expected_name)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("lease", ["metadata", "guard", "slot", "owner"])
def test_runtime_leases_reject_precreated_hostile_files(tmp_path, monkeypatch, kind, lease):
    """Capacity metadata and every lock class refuse substitutions without following them."""
    import garden.run_supervisor as supervisor

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = supervisor._private_runtime_dir()
    uid = os.getuid()
    target = tmp_path / "substitution-target"
    target.write_text("untouched")
    names = {
        "metadata": f"garden-heavy-test-{uid}-capacity.json",
        "guard": f"garden-heavy-test-{uid}-capacity.lock",
        "slot": f"garden-heavy-test-{uid}-0.lock",
        "owner": f"garden-heavy-test-{uid}-owner-{hashlib.sha256(b'owner').hexdigest()[:20]}.lock",
    }

    if lease == "slot":
        assert supervisor._authoritative_limit(1) == (1, None)
    path = root / names[lease]
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    run_dir = tmp_path / f"run-{lease}-{kind}"
    run_dir.mkdir()
    if lease == "owner":
        monkeypatch.setenv("GARDEN_EXECUTION_OWNER", "owner")

    def action() -> object:
        if lease in {"metadata", "guard"}:
            return supervisor._authoritative_limit(1)
        if lease == "slot":
            return supervisor._execution_slot(run_dir, lambda: False)
        return supervisor._execution_slot(run_dir, lambda: False, owner_scoped=True)

    with pytest.raises(RuntimeError, match="unsafe runtime file"):
        action()
    assert target.read_text() == "untouched"


def test_two_validations_from_one_worker_are_serialized(tmp_path):
    """Competing supported validation wrappers cannot multiply one worker's workload."""
    from garden.harness import Harness
    from garden.runs import Run

    workload = tmp_path / "workload.py"
    workload.write_text(
        "import fcntl, pathlib, time\n"
        "state = pathlib.Path('active.txt')\n"
        "with pathlib.Path('active.lock').open('a+') as lock:\n"
        " fcntl.flock(lock, fcntl.LOCK_EX)\n"
        " active, peak = map(int, (state.read_text() if state.exists() else '0 0').split())\n"
        " active += 1\n"
        " state.write_text(f'{active} {max(active, peak)}')\n"
        "time.sleep(0.25)\n"
        "with pathlib.Path('active.lock').open('a+') as lock:\n"
        " fcntl.flock(lock, fcntl.LOCK_EX)\n"
        " active, peak = map(int, state.read_text().split())\n"
        " state.write_text(f'{active - 1} {peak}')\n"
    )
    target = tmp_path / "test_workload.py"
    target.write_text(
        "import subprocess, sys\n\n"
        "def test_workload():\n"
        f" subprocess.run([sys.executable, {str(workload)!r}], check=True)\n"
    )
    validation = (
        '"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- '
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(str(target))} -q"
    )
    harness = Harness("nested", {"command": ["sh", "-c", f"{validation} & {validation} & wait"]})
    runner = LocalRunner({"timeout_minutes": 1}, harness)
    run_dir = tmp_path / "outer"
    run_dir.mkdir()
    brief = run_dir / "brief.md"
    brief.write_text("")
    run = Run(task_id="T-1", run_id="outer", dir=str(run_dir), runner="local")
    env = _synthetic_child_env(
        GARDEN_HEAVY_TEST_PARALLEL="1", XDG_RUNTIME_DIR=str(tmp_path),
        GARDEN_EXECUTION_CGROUP="", GARDEN_VALIDATION_RUNNER=sys.executable,
    )
    runner.launch(run, tmp_path, brief, env)

    os.waitpid(run.pid, 0)
    assert run.read_exit_code() == 0
    assert (tmp_path / "active.txt").read_text() == "0 1"
    statuses = list((run_dir / "validations").glob("*/execution.json"))
    assert len(statuses) == 2
    assert all(json.loads(path.read_text())["owner_scoped"] is True for path in statuses)


def test_waiting_supervisor_can_be_cancelled_without_leaking_lease(tmp_path):
    from garden.harness import Harness
    from garden.runs import Run

    h = Harness("tiny", {"command": ["sh", "-c", "sleep 1"]})
    runner = LocalRunner({"timeout_minutes": 0}, h)
    runs = []
    for number in (1, 2):
        d = tmp_path / f"cancel{number}"
        d.mkdir()
        brief = d / "brief.md"
        brief.write_text("")
        run = Run(task_id=f"T-{number}", run_id=f"c{number}", dir=str(d), runner="local")
        runner.launch(run, tmp_path, brief, _synthetic_child_env(
            GARDEN_HEAVY_TEST_PARALLEL="1", XDG_RUNTIME_DIR=str(tmp_path),
            GARDEN_HEAVY_EXECUTION="1",
        ))
        runs.append(run)
    deadline = time.monotonic() + 5
    while not all((r.path / "execution.json").exists() for r in runs) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all((r.path / "execution.json").exists() for r in runs)
    waiting = next(r for r in runs if '"state": "waiting"' in (r.path / "execution.json").read_text())
    assert waiting.stop(timeout=2)
    assert waiting.read_exit_code() == 143
    for run in runs:
        if run is not waiting:
            run.stop(timeout=2)


def test_setup_waits_inside_the_heavy_execution_budget(tmp_path, monkeypatch):
    """Setup is validation-capable, so it must not run before the supervisor's lease."""
    from garden.harness import Harness
    from garden.runs import Run

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = LocalRunner({"timeout_minutes": 0, "worker_env": {"pass": ["XDG_RUNTIME_DIR"]},
                          "setup": {"command": "touch setup-started; sleep 0.35"}},
                         Harness("tiny", {"command": ["true"]}))
    runs = []
    for number in (1, 2):
        worktree = tmp_path / f"wt{number}"
        worktree.mkdir()
        d = tmp_path / f"setup-run{number}"
        d.mkdir()
        run = Run(task_id=f"T-{number}", run_id=f"s{number}", dir=str(d), runner="local")
        runner.start(run, worktree, "")
        runs.append((run, worktree))

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        states = [json.loads((run.path / "execution.json").read_text()).get("state")
                  for run, _ in runs if (run.path / "execution.json").exists()]
        started = sum((worktree / "setup-started").exists() for _, worktree in runs)
        if sorted(states) == ["running", "waiting"] and started == 1:
            break
        time.sleep(0.01)
    assert sum((worktree / "setup-started").exists() for _, worktree in runs) == 1
    for run, _ in runs:
        os.waitpid(run.pid, 0)
    assert all((worktree / "setup-started").exists() for _, worktree in runs)


@pytest.mark.needs_remote_clone
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


@pytest.mark.needs_remote_clone
def test_ssh_runner_preserves_fractional_timeout_minutes(sched, fake_github):
    sched.cfg.data["products"]["demo"]["timeout_minutes"] = 0.5
    task = sched.store.task("DM-001")
    task.runner = "ssh"
    sched.store.save(task)

    # GNU timeout is an optional first line of defence for this controller-side SSH
    # process. Exercise command construction without assuming the macOS host ships it.
    with patch("garden.runner.ssh.shutil.which", return_value="/usr/bin/timeout"):
        sched.tick()

    assert "timeout 30 " in (sched.runs.latest("DM-001").path / "command.txt").read_text()


@pytest.mark.needs_remote_clone
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
    assert "GARDEN_VALIDATION_TIMEOUT_SECONDS=900" in remote_sh


@pytest.mark.needs_remote_clone
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


@pytest.mark.needs_remote_clone
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


@pytest.mark.needs_remote_clone
def test_ssh_remote_worker_installs_only_named_config_file(sched, garden, fake_github, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-tool.json"
    source.write_text("approved-tool-config")
    unrelated = tmp_path / "unrelated-instructions.md"
    unrelated.write_text("SECRET_SENTINEL_DENIED")
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg.setdefault("worker_env", {})["config_files"] = {
        "synthetic-tool": {"source": str(source), "destination": ".config/synthetic/tool.json",
                           "required": True},
    }
    cfg["products"]["demo"]["setup"] = {
        "command": "test \"$(cat \"$HOME/.config/synthetic/tool.json\")\" = approved-tool-config "
                   "&& test \"$(LC_ALL=C ls -ld \"$HOME/.config/synthetic/tool.json\" | cut -c1-10)\" "
                   "= -rw-------",
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    from garden.scheduler import Scheduler
    from garden.store import Store

    sc = Scheduler(Store(garden), github=fake_github, log=print)
    task = sc.store.task("DM-001")
    task.runner = "ssh"
    sc.store.save(task)
    sc.tick()
    run = sc.runs.latest("DM-001")
    _wait_for_child(run)

    assert run.read_exit_code() == 0
    public_outputs = [run.path / "brief.md", run.path / "stdout.json", run.path / "stderr.log",
                      garden / ".garden/events.jsonl", task.path]
    assert all("SECRET_SENTINEL_DENIED" not in path.read_text() for path in public_outputs if path.exists())


@pytest.mark.needs_remote_clone
def test_ssh_remote_worker_keeps_custom_config_dir_variable(sched, garden, fake_github, tmp_path, monkeypatch):
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg.setdefault("worker_env", {})["config_dirs"] = {"CUSTOM_HARNESS_HOME": "/srv/custom-creds"}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    from garden.scheduler import Scheduler
    from garden.store import Store

    sc = Scheduler(Store(garden), github=fake_github, log=print)
    dump = tmp_path / "worker-env.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_DUMP", str(dump))
    task = sc.store.task("DM-001")
    task.runner = "ssh"
    sc.store.save(task)
    sc.tick()
    _wait_for_child(sc.runs.latest("DM-001"))

    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert seen["CUSTOM_HARNESS_HOME"] == "/srv/custom-creds"


@pytest.mark.needs_remote_clone
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
