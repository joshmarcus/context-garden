"""Exercise transport loss with detached fake workers; no network or model calls."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from garden.harness import Harness
from garden.model import Status
from garden.proctree import pid_alive
from garden.runner.ssh import SSHRunner
from garden.runs import Run
from garden.scheduler.report import TickReport
from garden.ssh_transport import rpc

pytestmark = pytest.mark.needs_remote_clone


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("remote test did not reach the expected state before its deadline")


def reap_collector(run):
    try:
        os.waitpid(run.pid, 0)
    except ChildProcessError:
        pass  # subprocess bookkeeping may already have reaped the collector


def state(run):
    path = run.path / "ssh-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def start(sched, tmp_path, command="echo worker-complete", **options):
    task = sched.store.task("DM-001")
    runner = sched.runner_for(task, "ssh")
    runner.harness = Harness("fake", {"command": ["sh", "-c", command]})
    runner.config.update({"python": sys.executable, "poll_interval_seconds": 0.05,
                          "recovery_timeout_seconds": 1, "connect_timeout_seconds": 10,
                          "timeout_minutes": 0.3}, **options)
    run = sched.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = "boxA", task.default_branch(), "main"
    runner.start(run, tmp_path, "test brief")
    return runner, run


def disconnect_wrapper(tmp_path, action):
    path = tmp_path / "disconnect-ssh"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "script = sys.stdin.read()\n"
        "request = json.loads(script.splitlines()[-2])\n"
        "done = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True)\n"
        f"if request['action'] == {action!r}: sys.exit(255)\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n"
    )
    path.chmod(0o755)
    return path


def test_disconnect_after_launch_recovers_same_live_session(sched, tmp_path):
    started = tmp_path / "launch-count"
    runner, run = start(sched, tmp_path,
                        f"echo launch >> {shlex.quote(str(started))}; sleep 1; echo finished",
                        ssh_bin=str(disconnect_wrapper(tmp_path, "start")))
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    assert started.read_text() == "launch\n"
    assert "finished" in run.stdout_text()
    assert "SSH start exited 255" in (run.path / "transport.log").read_text()
    assert json.loads((run.path / "ssh-completion.json").read_text())["session"] == run.env_snapshot["ssh_tmux_session"]
    reap_collector(run)


def test_collector_death_never_completes_run_and_reconnect_recovers_commits(sched, tmp_path):
    marker = tmp_path / "ready"
    command = ("echo retained > recovered.txt; git add recovered.txt; "
               "git -c user.name=Test -c user.email=test@example.com commit -qm recovered; "
               f"touch {shlex.quote(str(marker))}; sleep 2; echo finished")
    runner, run = start(sched, tmp_path, command)
    wait_for(marker.exists)
    os.killpg(run.pid, signal.SIGKILL)
    reap_collector(run)
    assert not run.process_finished()
    restarted = SSHRunner(runner.config, runner.harness)
    assert restarted.reconcile(run) == ""
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    receipt = json.loads((run.path / "ssh-completion.json").read_text())
    remote = tmp_path / "remote-clone"
    assert subprocess.check_output(["git", "-C", str(remote), "show", run.branch + ":recovered.txt"], text=True) == "retained\n"
    assert receipt["head"]
    assert (Path(receipt["directory"]) / "brief.md").exists()
    reap_collector(run)


def test_unreachable_recovery_is_bounded_and_blocks_scheduler_retry(sched, tmp_path):
    wrapper = tmp_path / "unreachable-ssh"
    wrapper.write_text("#!/bin/sh\nexit 255\n")
    wrapper.chmod(0o755)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper))
    wait_for(lambda: state(run).get("status") == "held")
    reap_collector(run)
    assert not run.process_finished()
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test remote reservation")
    assert not sched._finished_or_timed_out(run, runner)
    assert sched.state.get(task.id)["needs_human"]["kind"] == "ssh_recovery"
    with pytest.raises(RuntimeError, match="remote worker outcome"):
        sched.retry(task)
    sched.reap_dead_runs(TickReport())
    assert Run.load(run.path).status == "running"
    assert not (run.path / "exit_code").exists()


def test_checkout_lease_refuses_second_writer_until_completion(sched, tmp_path):
    marker = tmp_path / "live"
    runner, first = start(sched, tmp_path, f"touch {shlex.quote(str(marker))}; sleep 2")
    wait_for(marker.exists)
    _other, second = start(sched, tmp_path, "echo must-not-run")
    wait_for(lambda: state(second).get("status") == "held")
    assert "leased by run" in state(second)["reason"]
    assert "must-not-run" not in second.stdout_text()
    wait_for(first.process_finished)
    reap_collector(first)
    reap_collector(second)


def test_stale_references_are_preserved_and_retry_uses_another_directory(sched, tmp_path):
    _runner, first = start(sched, tmp_path)
    wait_for(first.process_finished)
    first_dir = Path(state(first)["directory"])
    references = first_dir / "references"
    references.mkdir()
    old = references / "task.md"
    old.write_text("frozen prior brief")
    old.chmod(0o444)
    references.chmod(0o555)
    _runner, second = start(sched, tmp_path)
    wait_for(second.process_finished)
    assert second.read_exit_code() == 0
    assert state(second)["directory"] != str(first_dir)
    assert old.read_text() == "frozen prior brief"
    reap_collector(first)
    reap_collector(second)
    references.chmod(0o755)


def test_cancel_stops_exact_descendants_and_preserves_unrelated_process(sched, tmp_path):
    child_pid = tmp_path / "child-pid"
    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        runner, run = start(sched, tmp_path,
                            f"sleep 30 & echo $! > {shlex.quote(str(child_pid))}; wait")
        wait_for(child_pid.exists)
        descendant = int(child_pid.read_text())
        run.kill()
        wait_for(run.process_finished)
        assert run.read_exit_code() == 143
        assert not pid_alive(descendant)
        assert unrelated.poll() is None
        reap_collector(run)
    finally:
        unrelated.terminate()
        unrelated.wait()


def test_terminal_receipt_recovers_after_lost_ack(sched, tmp_path):
    runner, run = start(sched, tmp_path, ssh_bin=str(disconnect_wrapper(tmp_path, "ack")))
    wait_for(lambda: state(run).get("status") == "held")
    reap_collector(run)
    assert (run.path / "ssh-completion.json").exists()
    assert not run.process_finished()
    spec_path = run.path / "ssh-request.json"
    spec = json.loads(spec_path.read_text())
    spec["ssh"][0] = str(Path(__file__).parent / "fake_ssh.py")
    spec_path.write_text(json.dumps(spec))
    task = sched.store.task(run.task_id)
    sched.resume_ssh_collection(task)
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    assert run.stdout_text().count("worker-complete") == 1


def test_stale_ack_cannot_release_a_new_owners_checkout(sched, tmp_path):
    _runner, first = start(sched, tmp_path)
    wait_for(first.process_finished)
    _runner, second = start(sched, tmp_path, "sleep 1")
    wait_for(lambda: state(second).get("status") == "running")
    spec = json.loads((first.path / "ssh-request.json").read_text())
    assert rpc(spec, "ack", first.path)["status"] == "acknowledged"
    lease = Path(state(second)["directory"]).parent / "lease.json"
    assert json.loads(lease.read_text())["identity"] != spec["request"]["identity"]
    wait_for(second.process_finished)


def test_failure_diagnostic_uses_fatal_tail(sched, tmp_path):
    runner, run = start(sched, tmp_path,
                        "echo 'watcher unavailable; falling back' >&2; echo 'fatal preparation error' >&2; exit 2")
    wait_for(run.process_finished)
    assert run.read_exit_code() == 2
    assert runner.collect(run)["error"].endswith("fatal preparation error")


def test_failed_run_publishes_its_commits_and_keeps_its_own_diagnostic(sched, tmp_path):
    """A nonzero run must not leave commits reachable only inside the remote checkout."""
    command = ("echo kept > kept.txt; git add kept.txt; "
               "git -c user.name=Test -c user.email=test@example.com commit -qm kept; "
               "echo scratch > untracked.txt; echo 'fatal harness error' >&2; exit 2")
    runner, run = start(sched, tmp_path, command)
    wait_for(run.process_finished)
    assert run.read_exit_code() == 2
    assert runner.collect(run)["error"].endswith("fatal harness error")
    origin = tmp_path / "remote.git"
    for name in ("kept.txt", "untracked.txt"):
        assert subprocess.check_output(
            ["git", "-C", str(origin), "show", f"{run.branch}:{name}"], text=True).strip()
    assert (Path(state(run)["directory"]) / "publish.log").exists()
    reap_collector(run)


def test_acknowledged_run_directories_are_pruned_and_live_work_is_kept(sched, tmp_path):
    """Collected runs age out per checkout; an uncollected or live run keeps its artifacts."""
    _runner, first = start(sched, tmp_path, retain_runs=1)
    wait_for(first.process_finished)
    first_dir = Path(state(first)["directory"])
    (first_dir / "references").mkdir()
    (first_dir / "references" / "task.md").write_text("frozen prior brief")
    (first_dir / "references" / "task.md").chmod(0o444)
    (first_dir / "references").chmod(0o555)
    assert (first_dir / "acknowledged").exists()

    marker = tmp_path / "live"
    _runner, second = start(sched, tmp_path,
                            f"touch {shlex.quote(str(marker))}; sleep 2", retain_runs=1)
    wait_for(marker.exists)
    second_dir = Path(wait_for(lambda: state(second).get("directory")))
    assert first_dir.exists() and second_dir.exists()  # nothing is pruned while a run is live
    wait_for(second.process_finished)
    assert not first_dir.exists()  # the older acknowledged run aged out
    assert (second_dir / "brief.md").exists()  # the newest acknowledged run is retained
    reap_collector(first)
    reap_collector(second)


def test_remote_timeout_works_without_controller_connection(sched, tmp_path):
    runner, run = start(sched, tmp_path, "sleep 30", timeout_minutes=0.025)
    wait_for(lambda: state(run).get("status") == "running")
    remote_dir = Path(state(run)["directory"])
    os.killpg(run.pid, signal.SIGKILL)
    reap_collector(run)
    wait_for(lambda: (remote_dir / "completion.json").exists())
    receipt = json.loads((remote_dir / "completion.json").read_text())
    assert receipt["exit_code"] == 124
    assert "timed out" in receipt["reason"]
    runner.reconcile(run)
    wait_for(run.process_finished)
    assert run.read_exit_code() == 124
    reap_collector(run)


def test_large_output_is_recovered_without_loss_or_duplicate_bytes(sched, tmp_path):
    command = shlex.join([sys.executable, "-c", "print('x' * 1100000)"])
    runner, run = start(sched, tmp_path, command)
    wait_for(run.process_finished)
    assert run.stdout_text() == "x" * 1100000 + "\n"
    reap_collector(run)


def test_login_secrets_are_only_transferred_in_memory(sched, tmp_path, monkeypatch):
    secret = "synthetic-login-secret-not-for-disk"
    monkeypatch.setenv("UNRELATED_LOGIN_SECRET", secret)
    runner, run = start(sched, tmp_path)
    wait_for(run.process_finished)
    remote_dir = Path(state(run)["directory"])
    assert not (remote_dir / "context.pipe").exists()
    for path in remote_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), str(path)
    reap_collector(run)


def test_legacy_transport_exit_is_held_by_reap_and_dead_run_sweep(sched, tmp_path):
    runner = sched.runner_for(sched.store.task("DM-001"), "ssh")
    run = sched.runs.new_run("DM-001", "ssh")
    run.host = "boxA"
    run.save()
    (run.path / "exit_code").write_text("255")
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "legacy remote run")
    assert not sched._finished_or_timed_out(run, runner)
    sched.reap_dead_runs(TickReport())
    assert Run.load(run.path).status == "running"
    assert "legacy SSH" in sched.state.get(task.id)["ssh_recovery_hold"]


def test_log_exposes_attach_command_and_recovery_state(sched, tmp_path, garden):
    from tests.test_cli import run as cli

    runner, run = start(sched, tmp_path)
    wait_for(run.process_finished)
    output = cli(garden, "log", run.task_id)
    assert output.exit_code == 0, output.output
    # Rich wraps long session names, so compare the stable command and task separately.
    assert "tmux attach-session -r -t" in output.output
    assert "garden-DM-001" in output.output
    assert "SSH: terminal" in output.output
    reap_collector(run)


def test_attach_selects_only_the_exact_live_session_and_quotes_it(sched, garden, monkeypatch):
    """The CLI path does not pick a retry or let a recorded session alter its shell command."""
    from garden.cli import diagnostics

    run = sched.runs.new_run("DM-001", "ssh", run_id="attach-live")
    run.status, run.host = "running", "boxA"
    run.env_snapshot = {"ssh_tmux_session": "garden-safe; touch never"}
    run.save()
    store = sched.store

    command = diagnostics._attach_command(store, diagnostics._attach_run(store, "DM-001", ""))
    assert command[0] == str(store.config.get("ssh.ssh_bin"))
    assert command[-3:-1] == ["-tt", "boxA"]
    assert command[-1] == "tmux attach-session -r -t '=garden-safe; touch never'"

    completed = sched.runs.new_run("DM-001", "ssh", run_id="attach-finished")
    completed.status, completed.host = "done", "boxA"
    completed.env_snapshot = {"ssh_tmux_session": "garden-finished"}
    completed.save()
    with pytest.raises(ValueError, match="no longer running"):
        diagnostics._attach_run(store, "DM-001", completed.run_id)

    second = sched.runs.new_run("DM-001", "ssh", run_id="attach-second")
    second.status, second.host = "running", "boxA"
    second.env_snapshot = {"ssh_tmux_session": "garden-second"}
    second.save()
    with pytest.raises(ValueError, match="pass --run RUN_ID"):
        diagnostics._attach_run(store, "DM-001", "")


def test_attach_denies_another_member_and_never_launches_ssh(sched, garden, monkeypatch):
    import yaml

    from garden.cli import diagnostics
    from garden.members import MemberRegistry

    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["multiplayer"] = {"enabled": True}
    config_path.write_text(yaml.safe_dump(config))
    task_path = next((garden / "demo" / "p1" / "tasks").glob("DM-001-*.md"))
    task_path.write_text(task_path.read_text().replace("status: ready", "status: ready\nowner: alice"))
    registry = MemberRegistry(garden / ".garden")
    admin_token = registry.enroll_administrator("test-garden", "alice", "alice-cli")
    admin = registry.authenticate(admin_token)
    assert admin is not None
    registry.add_member(admin, "bob", "member", "all")
    monkeypatch.setenv("GARDEN_MEMBER_TOKEN", registry.issue_installation(admin, "bob", "bob-cli"))
    from garden.store import Store

    store = Store(garden)
    assert not diagnostics._attach_authorized(store, store.task("DM-001"))


def test_attach_requires_a_terminal_and_runs_only_the_selected_attempt(sched, garden, monkeypatch):
    from garden.cli import diagnostics
    from tests.test_cli import run as cli

    live = sched.runs.new_run("DM-001", "ssh", run_id="attach-cli")
    live.status, live.host = "running", "boxA"
    live.env_snapshot = {"ssh_tmux_session": "garden-attach-cli"}
    live.save()
    noninteractive = cli(garden, "attach", "DM-001")
    assert noninteractive.exit_code == 2
    assert "interactive local terminal" in noninteractive.output

    executed = []
    monkeypatch.setattr(diagnostics, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda command, check: executed.append(command)
                        or subprocess.CompletedProcess(command, 0))
    attached = cli(garden, "attach", "DM-001", "--run", live.run_id)
    assert attached.exit_code == 0, attached.output
    assert executed and executed[0][-1].endswith("=garden-attach-cli")


def test_attach_transport_joins_a_disposable_real_tmux_session(sched, garden, tmp_path):
    """A local SSH transport double proves the generated argv attaches without killing its pane."""
    from garden.cli import diagnostics

    if not Path("/usr/bin/tmux").exists():
        pytest.skip("real tmux is unavailable")
    run = sched.runs.new_run("DM-001", "ssh", run_id="attach-tmux")
    run.status, run.host = "running", "boxA"
    run.env_snapshot = {"ssh_tmux_session": "garden-attach-tmux"}
    run.save()
    socket_dir = Path(tempfile.mkdtemp(prefix="garden-tmux-", dir="/tmp"))
    environment = {"PATH": "/usr/bin:/bin", "TMUX_TMPDIR": str(socket_dir), "TERM": "xterm"}
    transport = tmp_path / "ssh-transport"
    transport.write_text("#!/bin/sh\nfor remote; do :; done\nexec sh -c \"$remote\"\n")
    transport.chmod(0o755)
    try:
        subprocess.run(["/usr/bin/tmux", "new-session", "-d", "-s", "garden-attach-tmux", "sleep 30"],
                       check=True, env=environment)
        command = diagnostics._attach_command(sched.store, run)
        command[0] = str(transport)
        attached = subprocess.Popen(["script", "-qefc", shlex.join(command), "/dev/null"],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.2)
        subprocess.run(["/usr/bin/tmux", "detach-client", "-s", "garden-attach-tmux"],
                       check=True, env=environment)
        _stdout, stderr = attached.communicate(timeout=5)
        assert attached.returncode == 0, stderr
        assert subprocess.run(["/usr/bin/tmux", "has-session", "-t", "garden-attach-tmux"],
                              env=environment, capture_output=True).returncode == 0
    finally:
        subprocess.run(["/usr/bin/tmux", "kill-session", "-t", "garden-attach-tmux"],
                       env=environment, check=False, capture_output=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_recovery_page_reconnects_same_run_without_offering_a_fresh_worker(sched, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    wrapper = tmp_path / "unavailable"
    wrapper.write_text("#!/bin/sh\nexit 255\n")
    wrapper.chmod(0o755)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper))
    wait_for(lambda: state(run).get("status") == "held")
    reap_collector(run)
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "remote recovery test")
    sched._finished_or_timed_out(run, runner)
    resumed = []
    monkeypatch.setattr(SSHRunner, "_start_collector", lambda self, record: resumed.append(record.run_id))
    with TestClient(create_app(sched.store, watch=False, host="testserver")) as client:
        for path in (f"/tasks/{task.id}", "/inbox"):
            page = client.get(path)
            assert page.status_code == 200
            assert f'/tasks/{task.id}/ssh-recover' in page.text
            assert "Reconnect to remote run" in page.text
            assert f'action="/tasks/{task.id}/retry"' not in page.text
            if path.startswith("/tasks/"):
                assert "tmux attach-session -r -t" in page.text
                assert run.env_snapshot["ssh_tmux_session"] in page.text
        response = client.post(f"/tasks/{task.id}/ssh-recover", follow_redirects=False)
        assert response.status_code == 303, response.text
    assert resumed == [run.run_id]
    assert len(sched.runs.runs_for(task.id)) == 1


def test_branch_drift_preserves_unrelated_commits(sched, tmp_path):
    runner, first = start(sched, tmp_path)
    wait_for(first.process_finished)
    wt = tmp_path / "remote-clone" / ".garden-worktrees" / first.task_id
    subprocess.run(["git", "-C", str(wt), "checkout", "-qb", "unrelated-work"], check=True)
    (wt / "keep.txt").write_text("unrelated")
    subprocess.run(["git", "-C", str(wt), "add", "keep.txt"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-qm", "unrelated"], check=True)
    original = subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"])
    runner, second = start(sched, tmp_path, "echo must-not-run")
    wait_for(second.process_finished)
    assert second.read_exit_code() == 4
    assert "branch drift" in runner.collect(second)["error"]
    assert subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"]) == original
    assert (wt / "keep.txt").read_text() == "unrelated"
