"""Bounded, restartable log collection for a durable remote tmux run."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .ssh_session import write_json

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


def rpc(spec: dict, action: str, directory: Path) -> dict:
    request = {**spec["request"], "action": action}
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
        raise RuntimeError(f"SSH {action} exited {done.returncode}: "
                           + (detail[-1][:500] if detail else "no transport diagnostic"))
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(f"SSH {action} returned no valid receipt") from exc
    if result.get("identity") != spec["request"]["identity"]:
        raise RuntimeError("SSH response belongs to a different run")
    return result


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
    """One collector at a time; a restarted controller resumes the same remote identity."""
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
        # deadline across restarts so repeatedly crashing collectors cannot poll forever.
        lost_since = float(state.get("lost_since") or time.time())
        first = not (directory / "ssh-launch-attempted").exists()
        while not (directory / "exit_code").exists():
            action = "start" if first else "cancel" if (directory / "ssh-cancel").exists() else "poll"
            if first:
                (directory / "ssh-launch-attempted").touch()
                first = False
            try:
                response = rpc(spec, action, directory)
                accept_logs(directory, response)
                state = {key: value for key, value in response.items() if key != "logs"}
                status = response["status"]
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
                    # existing owner or repeat a missing prerequisite. Present an operator hold.
                    state["status"] = "held"
                    write_json(directory / "ssh-rejected.json", response)
                    write_json(state_path, state)
                    return
                if status in {"running", "starting", "terminal"}:
                    lost_since = time.time()
                else:
                    state["lost_since"] = lost_since
                # The supervisor enforces the remote execution deadline. If its state is
                # stuck, bound the collector too, while retaining the checkout lease.
                if time.time() > spec["deadline"]:
                    state.update(status="held", reason="remote completion was not proven before recovery deadline")
                    write_json(state_path, state)
                    return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                state = {**state, "status": "recovering", "reason": str(exc), "lost_since": lost_since}
                with (directory / "transport.log").open("a") as log:
                    log.write(f"{time.time():.3f} {exc}\n")
            if time.time() - lost_since >= spec["recovery_timeout_seconds"]:
                state.update(status="held", reason="remote outcome uncertain: " + str(
                    state.get("reason") or "no authoritative completion or live session"))
            write_json(state_path, state)
            if state["status"] == "held":
                return
            time.sleep(spec["poll_interval_seconds"] if not state.get("more") else 0.01)


if __name__ == "__main__":
    monitor(Path(sys.argv[1]))
