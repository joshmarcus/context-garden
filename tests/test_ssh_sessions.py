"""Exercise transport loss with detached fake workers; no network or model calls."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
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


def start(sched, tmp_path, command="echo worker-complete", host="boxA", **options):
    task = sched.store.task("DM-001")
    runner = sched.runner_for(task, "ssh")
    runner.harness = Harness("fake", {"command": ["sh", "-c", command]})
    runner.config.update({"python": sys.executable, "poll_interval_seconds": 0.05,
                          "recovery_timeout_seconds": 1, "connect_timeout_seconds": 10,
                          "timeout_minutes": 0.3}, **options)
    run = sched.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = host, task.default_branch(), "main"
    runner.start(run, tmp_path, "test brief")
    return runner, run


def flaky_wrapper(tmp_path, name, down_marker):
    """A transport that fails transiently (ordinary loss, no permanent-failure pattern in its
    diagnostic) while `down_marker` exists, and proxies to a real `sh -s` once it is removed."""
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"if os.path.exists({str(down_marker)!r}):\n"
        "    sys.stderr.write('Connection timed out\\n')\n"
        "    sys.exit(255)\n"
        "script = sys.stdin.read()\n"
        "done = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True)\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n"
    )
    path.chmod(0o755)
    return path


def recording_wrapper(tmp_path, name, actions):
    """Proxy the transport while recording the RPC action outside captured stderr."""
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "script = sys.stdin.read()\n"
        "request = json.loads(script.splitlines()[-2])\n"
        f"with open({str(actions)!r}, 'a') as log: log.write(request['action'] + '\\n')\n"
        "done = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True)\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n"
    )
    path.chmod(0o755)
    return path


def make_legacy_held(run):
    """Turn a stopped test run into a serialized pre-artifact-receipt recovery record."""
    spec_path = run.path / "ssh-request.json"
    spec = json.loads(spec_path.read_text())
    spec["request"].pop("protocol_version", None)
    # A probe-only recovery must not execute either frozen module.  Real old modules are valid
    # but omit artifact receipts; invalid sentinels make accidental reuse fail deterministically.
    spec["request"]["source"] = {
        "ssh_session.py": "raise RuntimeError('legacy worker source was executed')\n",
        "proctree.py": "raise RuntimeError('legacy worker source was executed')\n",
    }
    spec_path.write_text(json.dumps(spec))
    current = state(run)
    (run.path / "ssh-state.json").write_text(json.dumps({
        **current,
        "status": "held",
        "reason": "legacy recovery requires an operator probe",
    }))


def identity_mismatch_wrapper(tmp_path):
    """Answers every call, but swaps in a fake identity so the collector's own reply-identity
    check fires — simulating a stale connection multiplexed to the wrong remote session."""
    path = tmp_path / "identity-mismatch-ssh"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "script = sys.stdin.read()\n"
        "done = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True)\n"
        "sys.stderr.write(done.stderr)\n"
        "if done.returncode == 0:\n"
        "    try:\n"
        "        payload = json.loads(done.stdout)\n"
        "        payload['identity'] = 'another-run-owns-this-session'\n"
        "        sys.stdout.write(json.dumps(payload))\n"
        "    except ValueError:\n"
        "        sys.stdout.write(done.stdout)\n"
        "else:\n"
        "    sys.stdout.write(done.stdout)\n"
        "sys.exit(done.returncode)\n"
    )
    path.chmod(0o755)
    return path


def absence_blip_wrapper(tmp_path, name, remaining_path, gate_path):
    """Proxies to a real `sh -s`, but once `gate_path` exists, overwrites a bounded number of
    consecutive poll/cancel replies to report every remote artifact absent — simulating exactly
    the kind of transient false negative the absence-confirmation threshold must tolerate,
    without actually destroying the remote checkout, run directory or tmux session."""
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "script = sys.stdin.read()\n"
        "request = json.loads(script.splitlines()[-2])\n"
        "done = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True)\n"
        "sys.stderr.write(done.stderr)\n"
        f"gate, remaining = {str(gate_path)!r}, {str(remaining_path)!r}\n"
        "left = int(open(remaining).read() or '0') if os.path.exists(remaining) else 0\n"
        "if request['action'] != 'start' and done.returncode == 0 and os.path.exists(gate) and left > 0:\n"
        "    payload = json.loads(done.stdout)\n"
        "    payload['status'] = 'unknown'\n"
        "    payload.pop('reason', None)\n"
        "    payload['artifacts'] = {'worktree_exists': False, 'directory_exists': False, 'session_exists': False}\n"
        "    payload['process_exists'] = False\n"
        "    open(remaining, 'w').write(str(left - 1))\n"
        "    sys.stdout.write(json.dumps(payload))\n"
        "else:\n"
        "    sys.stdout.write(done.stdout)\n"
        "sys.exit(done.returncode)\n"
    )
    path.chmod(0o755)
    return path


def destroy_remote_session(tmp_path, session):
    """Simulate a tmux session vanishing outright on the remote host (e.g. host replacement),
    against the fake, file-backed `tmux` fixture: kill the session's worker process and remove
    its on-disk record so a later `has-session` reports it gone, the way a real `tmux
    kill-session` would."""
    record = tmp_path / "tmux-sessions" / session
    if record.exists():
        pid = json.loads(record.read_text())["pid"]
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        record.unlink()
    for suffix in (".log", ".dead"):
        (tmp_path / "tmux-sessions" / (session + suffix)).unlink(missing_ok=True)


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


def test_disconnect_longer_than_former_recovery_window_reconnects_without_human(sched, tmp_path):
    """A cutoff that outlasts the old recovery window must never create a needs_human decision
    or require garden ssh-recover; the collector keeps retrying with growing backoff instead."""
    down = tmp_path / "down"
    down.touch()
    wrapper = flaky_wrapper(tmp_path, "unreachable-ssh", down)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper))
    first_attempt = wait_for(lambda: state(run).get("attempt") or None)
    # Outlast the former give-up threshold (recovery_timeout_seconds=1 from start()).
    wait_for(lambda: (state(run).get("attempt") or 0) > first_attempt)
    assert state(run).get("status") == "recovering"
    assert state(run).get("held_kind") is None
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test remote reservation")
    assert not sched._finished_or_timed_out(run, runner)
    assert "needs_human" not in sched.state.get(task.id)
    assert sched.state.get(task.id)["ssh_reconnect"]["attempt"] >= 1
    with pytest.raises(RuntimeError, match="remote worker outcome"):
        sched.retry(task)
    sched.reap_dead_runs(TickReport())
    assert Run.load(run.path).status == "running"
    assert not (run.path / "exit_code").exists()
    os.killpg(run.pid, signal.SIGKILL)
    reap_collector(run)
    down.unlink()


def test_repeated_controller_restarts_never_reset_backoff_into_a_tight_loop(sched, tmp_path):
    """A collector may be killed and restarted more than once, mirroring repeated controller
    restarts during a single outage. Each restart must resume the persisted attempt count
    rather than starting over at a tight retry cadence."""
    down = tmp_path / "down-restart"
    down.touch()
    wrapper = flaky_wrapper(tmp_path, "restart-ssh", down)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper), backoff_initial_seconds=0.5)
    wait_for(lambda: (state(run).get("attempt") or 0) >= 1)
    for _ in range(2):
        saved = state(run)
        last_attempt = saved.get("attempt")
        assert saved.get("next_retry_at", 0) > time.time()
        os.killpg(run.pid, signal.SIGKILL)
        reap_collector(run)
        assert not run.process_finished()
        restarted = SSHRunner(runner.config, runner.harness)
        assert restarted.reconcile(run) == ""
        # Restarting the controller must preserve the already scheduled delay instead of
        # immediately issuing another connection attempt and turning repeated restarts into
        # a tight loop.
        time.sleep(0.1)
        assert state(run).get("attempt") == last_attempt
        wait_for(lambda last=last_attempt: (state(run).get("attempt") or 0) > last)
    down.unlink()
    wait_for(run.process_finished)
    reap_collector(run)


def test_long_outage_followed_by_recovery_completes_automatically(sched, tmp_path):
    """Even a long outage must end in ordinary automatic completion once the host returns,
    with no operator action of any kind — distinct from merely outlasting the old window."""
    down = tmp_path / "down-long-outage"
    down.touch()
    wrapper = flaky_wrapper(tmp_path, "long-outage-ssh", down)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper))
    first_attempt = wait_for(lambda: state(run).get("attempt") or None)
    wait_for(lambda: (state(run).get("attempt") or 0) > first_attempt)
    assert state(run).get("status") == "recovering"
    down.unlink()
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    assert run.stdout_text().count("worker-complete") == 1
    assert state(run).get("last_success")
    assert state(run).get("last_error")
    reap_collector(run)


def test_cancellation_during_outage_is_recorded_and_delivered_once_reachable(sched, tmp_path):
    """An explicit cancellation while transport is down must be durable: recorded immediately
    and delivered to the same remote worker once the host is reachable again. Garden must not
    abandon or duplicate the remote worker in order to honor it."""
    down = tmp_path / "down-cancel"
    ready = tmp_path / "cancel-ready"
    wrapper = flaky_wrapper(tmp_path, "cancel-ssh", down)
    command = f"touch {shlex.quote(str(ready))}; sleep 30"
    runner, run = start(sched, tmp_path, command, ssh_bin=str(wrapper))
    wait_for(ready.exists)
    down.touch()  # transport drops only after the remote worker is confirmed running
    wait_for(lambda: state(run).get("status") == "recovering")
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test remote reservation")
    sched.cancel(task, "no longer needed")
    assert (run.path / "ssh-cancel").exists()
    assert Run.load(run.path).status == "running"  # ownership retained; not abandoned or duplicated
    assert sched.store.task(run.task_id).status == Status.CANCELLED
    down.unlink()  # host becomes reachable again; no operator action taken
    wait_for(run.process_finished)
    assert run.read_exit_code() == 143
    reap_collector(run)


def test_identity_mismatch_is_a_deterministic_terminal_hold(sched, tmp_path):
    """A stale connection that reaches a different run's remote session must stop immediately
    and hold for a person to investigate — retrying could only race the session's real owner."""
    wrapper = identity_mismatch_wrapper(tmp_path)
    runner, run = start(sched, tmp_path, ssh_bin=str(wrapper))
    wait_for(lambda: state(run).get("status") == "held")
    assert state(run).get("held_kind") == "identity_mismatch"
    reap_collector(run)
    assert not run.process_finished()
    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test remote reservation")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        sched.resume_ssh_collection(task)
    assert state(run).get("status") == "held"


def test_provider_proven_host_replacement_is_a_terminal_loss_not_a_reconnect(sched, tmp_path):
    """When every recorded remote artifact — the tmux session, the private run directory and
    the checkout worktree — is authoritatively confirmed gone (the opposite edge from ordinary
    transport loss: e.g. the host itself was replaced), Garden must record this as a terminal
    loss distinct from reconnecting: it releases the scheduler slot and does not start another
    implementation run in its place."""
    ready = tmp_path / "artifacts-absent-ready"
    # Loops until the tmux session is killed below, rather than a fixed sleep, so destroying
    # the session and its artifacts can never race a worker that finished on its own first.
    command = f"touch {shlex.quote(str(ready))}; while true; do sleep 0.05; done"
    runner, run = start(sched, tmp_path, command)
    wait_for(ready.exists)
    wait_for(lambda: state(run).get("status") == "running")
    remote_dir = Path(state(run)["directory"])
    session = run.env_snapshot["ssh_tmux_session"]
    destroy_remote_session(tmp_path, session)
    shutil.rmtree(remote_dir, ignore_errors=True)
    shutil.rmtree(tmp_path / "remote-clone" / ".garden-worktrees" / run.task_id, ignore_errors=True)
    wait_for(lambda: state(run).get("held_kind") == "remote_artifacts_absent")
    assert state(run)["status"] == "held"
    reap_collector(run)
    assert not run.process_finished()

    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test remote reservation")
    assert not sched._finished_or_timed_out(run, runner)
    assert Run.load(run.path).status == "failed"
    assert sched.store.task(run.task_id).status == Status.FAILED
    assert "needs_human" in sched.state.get(task.id)
    sched.reap_dead_runs(TickReport())
    assert len(sched.runs.runs_for(task.id)) == 1  # no automatic redispatch onto a fresh run


def test_legacy_recovery_probes_absence_without_replaying_start(sched, tmp_path):
    """An explicit recovery of a pre-receipt run uses current read-only probe code.  Even when
    every artifact is gone, it must never replay the frozen implementation launch."""
    actions = tmp_path / "legacy-actions"
    ready = tmp_path / "legacy-ready"
    wrapper = recording_wrapper(tmp_path, "recording-legacy-ssh", actions)
    runner, run = start(
        sched,
        tmp_path,
        f"touch {shlex.quote(str(ready))}; while true; do sleep 0.05; done",
        ssh_bin=str(wrapper),
    )
    wait_for(ready.exists)
    wait_for(lambda: state(run).get("status") == "running")
    remote_dir = Path(state(run)["directory"])
    session = run.env_snapshot["ssh_tmux_session"]
    os.killpg(run.pid, signal.SIGKILL)
    reap_collector(run)
    destroy_remote_session(tmp_path, session)
    shutil.rmtree(remote_dir)
    shutil.rmtree(tmp_path / "remote-clone" / ".garden-worktrees" / run.task_id)
    make_legacy_held(run)
    actions.write_text("")

    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test legacy remote reservation")
    sched.resume_ssh_collection(task)
    wait_for(lambda: state(run).get("held_kind") == "remote_artifacts_absent")
    recovered = Run.load(run.path)
    reap_collector(recovered)
    assert actions.read_text().splitlines() == ["poll", "poll", "poll"]
    assert state(run)["legacy_probe_only"] is True
    assert not sched._finished_or_timed_out(recovered, runner)
    assert Run.load(run.path).status == "failed"
    assert sched.store.task(run.task_id).status == Status.FAILED


def test_legacy_recovery_retains_a_surviving_worktree_without_start(sched, tmp_path):
    """A surviving exact artifact keeps ownership and its diagnostic; it is neither treated as
    terminal loss nor used as a reason to run the implementation again."""
    actions = tmp_path / "legacy-present-actions"
    ready = tmp_path / "legacy-present-ready"
    wrapper = recording_wrapper(tmp_path, "recording-legacy-present-ssh", actions)
    runner, run = start(
        sched,
        tmp_path,
        f"touch {shlex.quote(str(ready))}; while true; do sleep 0.05; done",
        ssh_bin=str(wrapper),
    )
    wait_for(ready.exists)
    wait_for(lambda: state(run).get("status") == "running")
    remote_dir = Path(state(run)["directory"])
    session = run.env_snapshot["ssh_tmux_session"]
    os.killpg(run.pid, signal.SIGKILL)
    reap_collector(run)
    destroy_remote_session(tmp_path, session)
    shutil.rmtree(remote_dir)
    make_legacy_held(run)
    actions.write_text("")

    task = sched.store.task(run.task_id)
    sched._transition(task, Status.RUNNING, "test legacy remote reservation")
    sched.resume_ssh_collection(task)
    observed = wait_for(
        lambda: state(run)
        if (state(run).get("artifacts", {}).get("worktree_exists")
            and "worktree_exists" in state(run).get("reason", "")) else None
    )
    assert observed["status"] == "held"
    assert observed["held_kind"] == "legacy_artifacts_present"
    assert observed["absence_streak"] == 0
    assert "worktree_exists" in observed["reason"]
    assert "start" not in actions.read_text().splitlines()
    assert not Run.load(run.path).process_finished()
    current = Run.load(run.path)
    wait_for(lambda: not pid_alive(current.pid))
    reap_collector(current)


def test_transient_absence_blip_below_the_confirmation_threshold_is_tolerated(sched, tmp_path):
    """A single false-negative artifact report — transient noise, not a real host replacement
    — must not be mistaken for a terminal remote_artifacts_absent loss. Only several
    consecutive authoritative confirmations end automatic collection; below that count,
    ordinary reconnection continues and the run still completes on its own."""
    ready = tmp_path / "blip-ready"
    gate = tmp_path / "blip-gate"
    remaining = tmp_path / "blip-remaining"
    remaining.write_text("2")
    wrapper = absence_blip_wrapper(tmp_path, "absence-blip-ssh", remaining, gate)
    runner, run = start(sched, tmp_path,
                        f"touch {shlex.quote(str(ready))}; sleep 2; echo worker-complete",
                        ssh_bin=str(wrapper))
    wait_for(ready.exists)
    wait_for(lambda: state(run).get("status") == "running")
    gate.touch()
    wait_for(lambda: (state(run).get("absence_streak") or 0) >= 1)
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    assert state(run).get("held_kind") is None
    assert run.stdout_text().count("worker-complete") == 1
    reap_collector(run)


def test_multiple_simultaneous_unavailable_hosts_respect_concurrency_bounds(sched, tmp_path):
    """A concurrency bound must be scoped per host as well as globally, and must not deadlock
    when every collector's last known state says "recovering" at once — e.g. right after every
    collector was killed by the same controller restart."""
    wrapper = tmp_path / "always-down"
    wrapper.write_text("#!/bin/sh\necho 'Connection timed out' >&2\nexit 255\n")
    wrapper.chmod(0o755)
    hosts = [{"name": "boxA", "host": "boxA", "repos": {"demo": str(tmp_path / "remote-clone")},
              "max_parallel": 2},
             {"name": "boxB", "host": "boxB", "repos": {"demo": str(tmp_path / "remote-clone")},
              "max_parallel": 1}]
    options = dict(ssh_bin=str(wrapper), hosts=hosts,
                   max_concurrent_reconnects_per_host=1, max_concurrent_reconnects=0)
    runner_a1, a1 = start(sched, tmp_path, **options)
    runner_a2, a2 = start(sched, tmp_path, **options)
    runner_b1, b1 = start(sched, tmp_path, host="boxB", **options)
    for run in (a1, a2, b1):
        wait_for(lambda r=run: state(r).get("status") == "recovering")
        os.killpg(run.pid, signal.SIGKILL)
        reap_collector(run)
    old_a2_pid = a2.pid

    active = sched.runs.active()
    assert runner_a1.reconcile(a1, active=active) == ""
    wait_for(lambda: state(a1).get("status") == "recovering" and a1.pid and pid_alive(a1.pid))

    active = sched.runs.active()
    assert runner_a2.reconcile(a2, active=active) == ""
    assert a2.pid == old_a2_pid and not pid_alive(a2.pid)  # deferred: boxA is already at its bound of 1

    active = sched.runs.active()
    assert runner_b1.reconcile(b1, active=active) == ""
    assert b1.pid and pid_alive(b1.pid)  # a different host is unaffected by boxA's bound

    os.killpg(a1.pid, signal.SIGKILL)
    reap_collector(a1)
    active = sched.runs.active()
    assert runner_a2.reconcile(a2, active=active) == ""
    assert a2.pid != old_a2_pid and pid_alive(a2.pid)  # the freed slot lets the deferred sibling restart

    os.killpg(a2.pid, signal.SIGKILL)
    reap_collector(a2)
    os.killpg(b1.pid, signal.SIGKILL)
    reap_collector(b1)


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


def test_terminal_completion_during_outage_is_collected_once_reachable(sched, tmp_path):
    """The remote worker can finish while transport is down; once it recovers, Garden must
    collect the same run's terminal receipt automatically — no operator resume call."""
    down = tmp_path / "down-ack"
    wrapper = flaky_wrapper(tmp_path, "lossy-ack", down)
    runner, run = start(sched, tmp_path, "sleep 0.5; echo worker-complete", ssh_bin=str(wrapper))
    wait_for(lambda: state(run).get("status") == "running")
    down.touch()  # transport drops while the remote worker is still finishing up
    wait_for(lambda: (state(run).get("attempt") or 0) >= 1)
    assert not run.process_finished()
    down.unlink()  # host becomes reachable again; no operator action taken
    wait_for(run.process_finished)
    assert run.read_exit_code() == 0
    assert run.stdout_text().count("worker-complete") == 1
    reap_collector(run)


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


def test_log_exposes_recovery_state(sched, tmp_path, garden):
    from tests.test_cli import run as cli

    runner, run = start(sched, tmp_path)
    wait_for(run.process_finished)
    output = cli(garden, "log", run.task_id)
    assert output.exit_code == 0, output.output
    assert "SSH: terminal" in output.output
    reap_collector(run)


def test_recovery_page_reconnects_same_run_without_offering_a_fresh_worker(sched, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    wrapper = tmp_path / "unavailable"
    wrapper.write_text("#!/bin/sh\necho 'Permission denied' >&2\nexit 255\n")
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
