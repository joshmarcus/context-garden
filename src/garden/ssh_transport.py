"""Bounded, restartable log collection for a durable remote tmux run."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .ssh_session import write_json


class IdentityMismatch(RuntimeError):
    """The remote host answered, but for a different run's identity.

    Retrying cannot help: another run now owns this checkout and tmux session, so this is a
    deterministic terminal condition rather than ordinary transport loss."""


class TransportRefused(RuntimeError):
    """ssh(1) itself refused the connection for a reason retrying will not fix."""


# ssh(1) exits 255 for its own failures (never the remote command's exit status). A small set
# of its diagnostics describe a permanent misconfiguration — a name that will not resolve, a
# credential that was revoked, a host key that no longer matches — that no amount of retrying
# repairs. Anything else at exit 255 (connection refused/reset, timeout, network unreachable)
# is ordinary transient loss and keeps reconnecting.
_PERMANENT_SSH_PATTERNS = (
    "could not resolve hostname",
    "permission denied",
    "host key verification failed",
    "no matching host key type",
    "no matching key exchange method",
    "no matching cipher found",
)

# A run once confirmed present (the remote reported a live session, a terminal receipt, or any
# artifact at all) that later reports all three artifacts absent, several times in a row, has
# not merely hit a network hiccup: the host was replaced or the run's remote state was removed
# outright. Requiring several consecutive confirmations — each a full round trip that returned
# valid, structured JSON — rules out one transient false report before treating this as terminal.
_ABSENCE_CONFIRMATIONS_REQUIRED = 3

# Executed from stdin, never from the worker's checkout or an installed remote Garden.
BOOTSTRAP = """
import io, json, sys, types
request = json.load(sys.stdin)
package = types.ModuleType('garden')
package.__path__ = []
sys.modules['garden'] = package
for name in ('proctree', 'ssh_session'):
    module = types.ModuleType('garden.' + name)
    module.__package__ = 'garden'
    sys.modules[module.__name__] = module
    exec(compile(request['source'][name + '.py'], name + '.py', 'exec'), module.__dict__)
sys.stdin = io.StringIO(json.dumps(request))
module.main()
"""


def _controller_probe_source() -> dict[str, str]:
    """Current trusted probe code for inspecting artifacts left by an older run.

    A durable request deliberately freezes the worker runtime used at launch.  That is the
    right source for starting and acknowledging its worker, but an older snapshot cannot
    report fields added after it was dispatched.  A recovery probe is read-only and executes
    from stdin, outside the checkout, so the controller's current source can safely inspect
    the exact recorded identity without changing or executing the frozen worker runtime.
    """
    root = Path(__file__).parent
    return {name: (root / name).read_text() for name in ("ssh_session.py", "proctree.py")}


def rpc(spec: dict, action: str, directory: Path, *, controller_probe: bool = False) -> dict:
    request = {**spec["request"], "action": action}
    if controller_probe:
        request["source"] = _controller_probe_source()
    request["offsets"] = {name: (directory / name).stat().st_size if (directory / name).exists() else 0
                          for name in ("stdout.json", "stderr.log")}
    if action == "start":
        request["api_key"] = os.environ.get(spec["api_key_env"], "")
    script = (f"exec {shlex.quote(spec['python'])} -c {shlex.quote(BOOTSTRAP)} "
              "<<'GARDEN_SSH_REQUEST'\n" + json.dumps(request) + "\nGARDEN_SSH_REQUEST\n")
    done = subprocess.run([*spec["ssh"], "sh", "-s"], input=script, capture_output=True,
                          text=True, timeout=spec["connect_timeout_seconds"])
    if done.returncode:
        detail = done.stderr.strip().splitlines()
        message = detail[-1][:500] if detail else "no transport diagnostic"
        if done.returncode == 255 and any(p in message.lower() for p in _PERMANENT_SSH_PATTERNS):
            raise TransportRefused(f"SSH {action} was permanently refused: {message}")
        raise RuntimeError(f"SSH {action} exited {done.returncode}: {message}")
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(f"SSH {action} returned no valid receipt") from exc
    if result.get("identity") != spec["request"]["identity"]:
        raise IdentityMismatch(
            f"expected identity {spec['request']['identity']!r}, remote reported {result.get('identity')!r}"
        )
    return result


def _backoff_seconds(spec: dict, attempt: int) -> float:
    """Bounded exponential backoff with jitter for the `attempt`-th consecutive transport
    failure (0-indexed). Jitter (50%-100% of the bound) keeps many collectors that lost
    transport at the same moment from all retrying in lockstep."""
    initial = float(spec.get("backoff_initial_seconds") or spec["poll_interval_seconds"])
    multiplier = float(spec.get("backoff_multiplier") or 2.0)
    cap = float(spec["recovery_timeout_seconds"])
    bound = min(cap, initial * (multiplier**attempt))
    return bound * (0.5 + random.random() * 0.5)


def accept_logs(directory: Path, response: dict) -> None:
    for name in ("stdout.json", "stderr.log"):
        part = response.get("logs", {}).get(name)
        if part:
            path = directory / name
            with path.open("ab") as output:
                if output.tell() != part["offset"]:
                    raise RuntimeError("SSH log offset mismatch; refusing duplicated output")
                output.write(base64.b64decode(part["data"], validate=True))


def monitor(directory: Path) -> None:
    """One collector at a time; a restarted controller resumes the same remote identity.

    Ordinary transport loss — a dropped connection, a collector crash, a controller restart —
    retries forever with bounded, jittered exponential backoff (`attempt`, `last_error`,
    `next_retry_at` in `ssh-state.json`); it never asks a person to resume it. Only an
    authoritative remote refusal, an identity mismatch (another run now owns this session), or
    a proven-permanent transport failure ends collection — `held_kind` in the state records
    which, so a caller can tell "still trying" from "will not resolve on its own"."""
    with (directory / "ssh-monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        spec = json.loads((directory / "ssh-request.json").read_text())
        state_path = directory / "ssh-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("status") in {"held", "terminal"}:
            return
        # A killed collector is itself a transport loss. Preserve the first uncertainty
        # timestamp and the backoff attempt count across restarts so a repeatedly crashing
        # collector resumes its schedule instead of retrying in a tight loop.
        lost_since = float(state.get("lost_since") or time.time())
        attempt = int(state.get("attempt") or 0)
        last_success = state.get("last_success")
        last_error = state.get("last_error")
        confirmed_present = bool(state.get("confirmed_present"))
        absence_streak = int(state.get("absence_streak") or 0)
        legacy_probe_only = bool(state.get("legacy_probe_only"))
        first = not legacy_probe_only and not (directory / "ssh-launch-attempted").exists()
        while not (directory / "exit_code").exists():
            # A controller restart must not turn a persisted backoff delay into an immediate
            # connection attempt.  The collector can safely sleep here — it is detached from
            # the scheduler — and cancellation still bypasses the wait so its intent can be
            # delivered at the first possible contact.
            retry_at = float(state.get("next_retry_at") or 0)
            if state.get("status") == "recovering" and not (directory / "ssh-cancel").exists():
                delay = retry_at - time.time()
                if delay > 0:
                    time.sleep(delay)
                    continue
            action = "start" if first else "cancel" if (directory / "ssh-cancel").exists() else "poll"
            if first:
                (directory / "ssh-launch-attempted").touch()
                first = False
            try:
                response = rpc(
                    spec, action, directory,
                    controller_probe=legacy_probe_only and action != "start",
                )
                accept_logs(directory, response)
                state = {key: value for key, value in response.items() if key != "logs"}
                if legacy_probe_only:
                    state["legacy_probe_only"] = True
                last_success = time.time()
                status = response["status"]
                artifacts = response.get("artifacts") or {}
                process_exists = response.get("process_exists")
                if (status in {"starting", "running", "terminal"}
                        or any(artifacts.values()) or process_exists is True):
                    confirmed_present, absence_streak = True, 0
                elif (action != "start" and artifacts and not any(artifacts.values())
                      and process_exists is False and (confirmed_present or legacy_probe_only)):
                    # This identity was proven present at least once, and the remote now
                    # authoritatively reports its run directory, checkout worktree and tmux
                    # session all gone — not a network hiccup, since this reply is itself a
                    # valid, structured round trip. Several such reports in a row rule out one
                    # noisy false negative before treating this as an unrecoverable terminal loss.
                    absence_streak += 1
                    if absence_streak >= _ABSENCE_CONFIRMATIONS_REQUIRED:
                        state.update(
                            status="held", held_kind="remote_artifacts_absent",
                            absence_streak=absence_streak, last_success=last_success,
                            reason=(
                                "the remote run directory, checkout worktree, tmux session, and "
                                "recorded supervisor process "
                                f"were all confirmed absent after {absence_streak} consecutive "
                                "checks; treating this identity as a terminal loss rather than "
                                "retrying or starting another implementation run"
                            ),
                        )
                        write_json(state_path, state)
                        return
                if (action != "start" and status == "unknown" and not response.get("reason")
                        and not confirmed_present and not legacy_probe_only):
                    # A bare "unknown" with no vanished-session reason means this identity was
                    # never actually launched — most likely transport loss ate every reply to
                    # the original "start" before this collector ever marked one attempted.
                    # `launch()` is idempotent, so retrying "start" here is always safe: it
                    # either creates the session for the first time or is a no-op that returns
                    # the current snapshot of a session that already exists. Once this identity
                    # has ever been confirmed present, a matching bare "unknown" instead feeds
                    # the absence-streak check above rather than this branch.
                    (directory / "ssh-launch-attempted").unlink(missing_ok=True)
                    first = True
                if legacy_probe_only and status == "unknown":
                    present = sorted(name for name, exists in artifacts.items() if exists)
                    if process_exists is True:
                        present.append("recorded_process_exists")
                    detail = (
                        "legacy recovery retained ownership because remote artifacts remain: "
                        + ", ".join(present)
                        if present else
                        "legacy recovery is confirming that every remote artifact is absent"
                    )
                    if present:
                        state.update(
                            status="held",
                            held_kind="legacy_artifacts_present",
                            reason=detail,
                        )
                    else:
                        state.update(status="recovering", reason=response.get("reason") or detail)
                state["confirmed_present"] = confirmed_present
                state["absence_streak"] = absence_streak
                state["last_success"] = last_success
                state["last_error"] = last_error
                attempt = 0  # any successful call proves transport is up, whatever the remote reports
                state["attempt"] = attempt
                state["next_retry_at"] = None
                if state.get("held_kind") == "legacy_artifacts_present":
                    write_json(state_path, state)
                    return
                if status == "terminal" and not response.get("more"):
                    write_json(directory / "ssh-completion.json", state)
                    # Persist the complete receipt before releasing remote checkout ownership.
                    # Repeating this acknowledgement after a crash is idempotent.
                    rpc(spec, "ack", directory)
                    state["status"] = "terminal"
                    write_json(state_path, state)
                    (directory / "exit_code").write_text(str(response["exit_code"]))
                    return
                if status in {"blocked", "rejected"}:
                    # No owned worker was launched, but retrying automatically could race an
                    # existing owner or repeat a missing prerequisite. This is an authoritative
                    # remote refusal, not ordinary transport loss: present an operator hold.
                    state["status"] = "held"
                    state["held_kind"] = "authoritative_refusal"
                    write_json(directory / "ssh-rejected.json", response)
                    write_json(state_path, state)
                    return
                if status in {"running", "starting", "terminal"}:
                    lost_since = time.time()
                else:
                    state["lost_since"] = lost_since
                write_json(state_path, state)
            except IdentityMismatch as exc:
                state = {**state, "status": "held", "held_kind": "identity_mismatch", "reason": str(exc)}
                write_json(state_path, state)
                return
            except TransportRefused as exc:
                state = {**state, "status": "held", "held_kind": "transport_refused", "reason": str(exc)}
                write_json(state_path, state)
                return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                attempt += 1
                sleep_for = _backoff_seconds(spec, attempt - 1)
                last_error = str(exc)
                state = {**state, "status": "recovering", "reason": str(exc), "lost_since": lost_since,
                         "attempt": attempt, "last_error": last_error, "last_success": last_success,
                         "next_retry_at": time.time() + sleep_for}
                with (directory / "transport.log").open("a") as log:
                    log.write(f"{time.time():.3f} {exc}\n")
                write_json(state_path, state)
                time.sleep(sleep_for)
                continue
            time.sleep(spec["poll_interval_seconds"] if not state.get("more") else 0.01)


if __name__ == "__main__":
    monitor(Path(sys.argv[1]))
