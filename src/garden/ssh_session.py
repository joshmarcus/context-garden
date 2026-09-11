"""Small, stdlib-only remote tmux supervisor, shipped by the trusted SSH runner.

The transport can disappear at any point. Only this supervisor writes completion;
the checkout lease survives until the controller acknowledges that exact receipt.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from .proctree import descendants, direct_children, pid_alive, process_group_alive

CHUNK_BYTES = 256 * 1024


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("SSH control directory must be a real, user-owned 0700 directory")


def locations(request: dict) -> tuple[Path, Path, str]:
    repo = Path(request["repo"])
    if not repo.is_absolute() or repo.resolve() != repo:
        raise RuntimeError("SSH repository must be an absolute canonical path")
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo,
                            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    root = (repo / common).resolve() / "garden-ssh"
    private_dir(root)
    checkout = repo if request["in_place"] else repo / ".garden-worktrees" / request["task"]
    identity = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:20]
    root = root / identity
    private_dir(root)
    run_key = hashlib.sha256(request["identity"].encode()).hexdigest()[:24]
    directory = root / run_key
    label = re.sub(r"[^a-zA-Z0-9_-]", "-", request["task"])[:40]
    return root, directory, f"garden-{label}-{run_key}"


def snapshot(request: dict) -> dict:
    root, directory, session = locations(request)
    result = {"identity": request["identity"], "session": session,
              "directory": str(directory), "status": "unknown"}
    for name in ("completion.json", "state.json"):
        path = directory / name
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get("identity") != request["identity"]:
                raise RuntimeError("SSH receipt identity mismatch")
            result.update(saved)
            break
    # Session existence alone is not completion. A vanished supervisor without a receipt
    # remains uncertain even if there is no process to find.
    if result["status"] in {"running", "starting"}:
        probe = subprocess.run(["tmux", "display-message", "-p", "-t", session + ":",
                                "#{pane_dead}"], capture_output=True, text=True, timeout=5)
        if probe.returncode or probe.stdout.strip() != "0":
            result.update(status="unknown", reason="tmux session vanished without completion")
    result["logs"] = {}
    result["more"] = False
    for name in ("stdout.json", "stderr.log"):
        path = directory / name
        offset = max(0, int(request.get("offsets", {}).get(name, 0)))
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(CHUNK_BYTES)
            result["logs"][name] = {"offset": offset, "data": base64.b64encode(chunk).decode()}
            result["more"] |= path.stat().st_size > offset + len(chunk)
    return result


def launch(request: dict) -> dict:
    root, directory, session = locations(request)
    if not shutil.which("tmux"):
        return {"identity": request["identity"], "status": "rejected",
                "reason": "SSH workers require tmux on the remote host"}
    # The guard serializes both creation and acknowledgement. A lease with incomplete
    # metadata is deliberately not stealable after an interrupted launch.
    with (root / "guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        lease = root / "lease.json"
        if lease.exists():
            owner = json.loads(lease.read_text())
            if owner.get("identity") == request["identity"]:
                return snapshot(request)
            return {"identity": request["identity"], "status": "blocked",
                    "reason": f"remote checkout is leased by run {owner.get('run_id', 'unknown')}"}
        if directory.exists():
            # Replaying a collected run must never execute the implementation again.
            return snapshot(request)
        private_dir(directory)
        write_json(lease, {"identity": request["identity"], "run_id": request["run_id"]})
        request = dict(request)
        key = request.pop("api_key", "")
        source = request.pop("source")
        package = directory / "runtime"
        private_dir(package)
        package = package / "garden"
        private_dir(package)
        (package / "__init__.py").write_text("")
        for name in ("ssh_session.py", "proctree.py"):
            (package / name).write_text(source[name])
        request["directory"] = str(directory)
        request["session"] = session
        write_json(directory / "request.json", request)
        write_json(directory / "state.json", {"identity": request["identity"],
                                             "status": "starting", "session": session})
        fifo = directory / "context.pipe"
        os.mkfifo(fifo, 0o600)
        # Neither API keys nor the login environment are put in files, tmux options or
        # process arguments. The one-use pipe supplies this launch's fresh environment.
        command = (f"cd {shlex.quote(str(directory / 'runtime'))} && "
                   f"exec {shlex.quote(sys.executable)} -m garden.ssh_session run "
                   f"{shlex.quote(str(directory))}")
        safe_env = {key: value for key, value in os.environ.items()
                    if key in {"HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "TMUX_TMPDIR"}}
        started = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, command, ";",
             "set-option", "-w", "-t", session + ":", "remain-on-exit", "on"],
            capture_output=True, text=True, env=safe_env, timeout=10,
        )
        if started.returncode:
            write_json(directory / "state.json", {"identity": request["identity"],
                       "status": "unknown", "reason": "tmux launch did not acknowledge success: "
                       + started.stderr.strip()[-500:]})
            return snapshot(request)
        payload = json.dumps({"env": dict(os.environ), "api_key": key}).encode()
        # The reader has its own deadline. Nonblocking writes bound the bootstrap even
        # if the session dies between starting and receiving its context.
        deadline = time.monotonic() + 10
        fd = None
        try:
            while time.monotonic() < deadline:
                try:
                    if fd is None:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    if payload:
                        payload = payload[os.write(fd, payload):]
                    if not payload:
                        break
                except (BlockingIOError, OSError):
                    pass
                time.sleep(0.02)
        finally:
            if fd is not None:
                os.close(fd)
    return snapshot(request)


def remove_tree(path: Path) -> None:
    """Delete a run directory even though a reference snapshot inside it is read-only.

    A read-only file in a writable directory is still removable, so making the directories
    writable is enough; symlinks are left alone so nothing outside the tree is touched.
    """
    for parent, directories, _files in os.walk(path, topdown=False):
        for name in directories:
            target = Path(parent) / name
            if target.is_symlink():
                continue
            try:
                target.chmod(0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def prune(root: Path, keep: Path, retain: int) -> list[str]:
    """Drop the oldest acknowledged run directories, keeping `retain` of the newest.

    Only a run whose exact completion the controller has already acknowledged is removable:
    a live, uncertain or unacknowledged run keeps its brief, reference snapshot and logs
    until a person or a later collector settles it. Called with the checkout guard held.
    """
    if retain < 1:
        return []
    settled = sorted(
        (path for path in root.iterdir()
         if path.is_dir() and path != keep and (path / "acknowledged").exists()),
        key=lambda path: path.stat().st_mtime,
    )
    removed = []
    for path in settled[:max(0, len(settled) + 1 - retain)]:
        remove_tree(path)
        removed.append(path.name)
    return removed


def acknowledge(request: dict) -> dict:
    root, directory, _session = locations(request)
    with (root / "guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        completion = json.loads((directory / "completion.json").read_text())
        if completion.get("identity") != request["identity"] or completion.get("status") != "terminal":
            raise RuntimeError("cannot acknowledge an unverified remote completion")
        (directory / "acknowledged").touch()
        lease = root / "lease.json"
        if lease.exists() and json.loads(lease.read_text()).get("identity") == request["identity"]:
            lease.unlink()
        pruned = prune(root, directory, int(request.get("retain_runs") or 1))
    return {"identity": request["identity"], "status": "acknowledged", "pruned": pruned}


def read_context(directory: Path) -> dict:
    path = directory / "context.pipe"
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    chunks = bytearray()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            try:
                data = os.read(fd, 65536)
                if data:
                    chunks.extend(data)
                    continue
                if chunks:
                    return json.loads(chunks)
            except BlockingIOError:
                pass
            time.sleep(0.02)
        raise RuntimeError("SSH launch context was not delivered before its deadline")
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def display(chunk: bytes) -> None:
    """Show useful stream events in tmux; raw bytes remain in the durable log."""
    for line in chunk.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict) and "message" in event:
                parts = event["message"].get("content", [])
                line = "\n".join(str(part.get("text") or (
                    f"{part.get('name')}: {part.get('input')}" if part.get("type") == "tool_use" else ""
                )) for part in parts if isinstance(part, dict))
        except (ValueError, AttributeError):
            pass
        if line:
            # Do not replay terminal controls from tool output into the observer's terminal.
            print("".join(c for c in line[:4000] if c.isprintable() or c == "\n"), flush=True)


def stop_children(child: subprocess.Popen) -> bool:
    """Signal only this supervisor's live descendants and its child's owned group."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in reversed(descendants(os.getpid())):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if process_group_alive(child.pid):
            try:
                os.killpg(child.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            child.poll()
            for pid in direct_children(os.getpid()):
                if pid != child.pid:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            if not process_group_alive(child.pid) and not any(
                pid_alive(pid) for pid in descendants(os.getpid())
            ):
                return True
            time.sleep(0.05)
    return False


def supervise(directory: Path) -> None:
    request = json.loads((directory / "request.json").read_text())
    identity = request["identity"]
    if sys.platform == "linux":
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # adopt descendants which detach with setsid
    state = {"identity": identity, "status": "running", "session": request["session"]}
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    child = None
    rc, reason = 1, ""
    print(f"Garden {request['task']} / {request['run_id']}\nDetach with Ctrl-b d.", flush=True)
    try:
        context = read_context(directory)
        script = request["script"].replace("__GARDEN_HARNESS_API_KEY_VALUE__",
                                            shlex.quote(context["api_key"]))
        script = "GARDEN_RUN_DIR=" + shlex.quote(str(directory)) + "\n" + script
        # stdin may contain a large reference snapshot; use a separate temporary pipe writer
        # via communicate in a thread so neither stdin nor worker output can deadlock launch.
        child = subprocess.Popen(["sh", "-s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=context["env"], start_new_session=True)
        import threading

        def feed():
            try:
                child.stdin.write(script.encode())
                child.stdin.close()
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        state["pid"] = child.pid
        write_json(directory / "state.json", state)
        deadline = time.monotonic() + request["timeout_seconds"]
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((child.stdout, "stdout.json"), (child.stderr, "stderr.log")):
                selector.register(pipe, selectors.EVENT_READ, name)
            while child.poll() is None or selector.get_map():
                if stopping or (directory / "cancel").exists() or time.monotonic() > deadline:
                    rc = 143 if stopping or (directory / "cancel").exists() else 124
                    reason = "worker cancelled" if rc == 143 else "remote worker timed out"
                    break
                for key, _mask in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    with (directory / key.data).open("ab") as log:
                        log.write(chunk)
                    display(chunk)
                # Detached descendants retaining stdout must not keep a finished parent
                # alive forever; cleanup below owns those descendants too.
                if child.poll() is not None:
                    rc = child.returncode
                    if not selector.select(0):
                        break
        if not reason:
            rc = child.wait(timeout=5)
    except Exception as exc:
        reason = f"remote supervisor: {type(exc).__name__}: {exc}"
    finally:
        clean = child is None or stop_children(child)
        if child is not None:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                clean = False
        if not clean:
            reason = "remote descendant cleanup could not confirm termination"
        if reason:
            with (directory / "stderr.log").open("a") as log:
                log.write(reason + "\n")
        result = {**state, "status": "terminal" if clean else "unknown",
                  "exit_code": rc, "reason": reason, "finished_at": time.time()}
        wt = Path(request["repo"]) if request["in_place"] else (
            Path(request["repo"]) / ".garden-worktrees" / request["task"])
        for field, command in (("head", ["rev-parse", "HEAD"]),
                               ("branch", ["branch", "--show-current"]),
                               ("dirty", ["status", "--porcelain"])):
            try:
                result[field] = subprocess.run(["git", "-C", str(wt), *command],
                    text=True, capture_output=True, timeout=10, check=True).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                result[field] = ""
        write_json(directory / ("completion.json" if clean else "state.json"), result)
        print(f"\nGarden finished: exit {rc}. {reason}\nWork and logs retained in {directory}.", flush=True)


def main() -> None:
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        supervise(Path(sys.argv[2]))
        return
    request = json.load(sys.stdin)
    action = request["action"]
    if action == "start":
        result = launch(request)
    elif action == "ack":
        result = acknowledge(request)
    else:
        if action == "cancel":
            _root, directory, _session = locations(request)
            if directory.is_dir():
                (directory / "cancel").touch()
        result = snapshot(request)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
