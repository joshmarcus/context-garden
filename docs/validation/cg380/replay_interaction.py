"""Replay the bounded CG-380 served-app flow and retain reviewable HTTP evidence."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from reproduce import make_garden, pressure


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_event(base_url: str, path: str, state: str, expected: int) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.perf_counter()
    try:
        response = urllib.request.urlopen(f"{base_url}{path}", timeout=10)
        status = response.status
        body_bytes = len(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        body_bytes = len(exc.read())
    assert status == expected
    return {
        "at": started_at,
        "state": state,
        "action": "HTTP request",
        "method": "GET",
        "url": f"{base_url}{path}",
        "status": status,
        "elapsed_s": time.perf_counter() - started,
        "observed_consequence": f"received {status} with {body_bytes} response bytes",
    }


def run_state(source: Path, garden: Path, output: Path, slots: int) -> dict[str, Any]:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    profile_log = output / f"slots-{slots}-spans.jsonl"
    server_log_path = output / f"slots-{slots}-server.log"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).parent))),
        "CG380_GARDEN": str(garden),
        "CG380_PROFILE_LOG": str(profile_log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "serve_profiled:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
    ]
    server_log = server_log_path.open("w")
    server = subprocess.Popen(
        command,
        cwd=Path(__file__).parent,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    load_env = {**os.environ, "CG380_FIXTURE_SECONDS": "120"}
    loads = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "reproduce.py"), "--fixture", "session"],
            env=load_env,
            start_new_session=True,
        )
        for _ in range(slots)
    ]
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base_url}/api/tasks", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"served app did not become ready; see {server_log_path}")

        label = "empty_worker_occupancy" if slots == 0 else "four_replay_occupants"
        events = []
        time.sleep(1.1)
        for temperature in ("cold", "warm"):
            for path in ("/now1", "/inbox", "/config"):
                events.append(request_event(base_url, path, f"{label}:{temperature}", 200))
        events.append(request_event(base_url, "/cg380-intentional-missing", f"{label}:failure", 404))
        events.append(request_event(base_url, "/now1", f"{label}:recovery", 200))
        tick = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "profile_tick.py")],
            env=env,
            check=True,
            text=True,
            capture_output=True,
        )
        events.append(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "state": f"{label}:background_tick",
                "action": "bounded no-dispatch scheduler tick",
                "status": tick.returncode,
                "observed_consequence": "tick completed and emitted phase timing",
            }
        )
        return {
            "state": label,
            "slots": slots,
            "server": {
                "pid": server.pid,
                "cgroup": Path(f"/proc/{server.pid}/cgroup").read_text().strip(),
                "command": command,
                "log": str(server_log_path),
            },
            "replay_processes": [
                {"pid": child.pid, "cgroup": Path(f"/proc/{child.pid}/cgroup").read_text().strip()}
                for child in loads
            ],
            "events": events,
            "tick": json.loads(tick.stdout),
        }
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server_log.close()
        for child in loads:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    garden = output / "garden-1000"
    make_garden(garden, tasks_count=1000)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    initial = pressure()
    report = {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
        "environment": {
            "python": sys.version,
            "fixture": {"tasks": 1000, "runs": 1549, "events": 12500},
            "caps": initial,
            "representativeness": (
                "Deterministic 32 MiB CPU/sleep subprocesses represent model-session occupancy, "
                "not vendor, network, or production traffic."
            ),
        },
        "command": " ".join(sys.argv),
        "started_at": started_at,
        "states": ["empty_worker_occupancy", "four_replay_occupants", "failure", "recovery"],
        "events": [],
        "runs": [],
    }
    for slots in (0, 4):
        run = run_state(source, garden, output, slots)
        report["runs"].append(run)
        report["events"].extend(run["events"])
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["final_cgroup"] = pressure()
    report["limitations"] = (
        "Bounded serial localhost replay on a disposable garden. It verifies served behavior "
        "and evidence structure; the retained full report remains the attribution dataset."
    )
    (output / "interaction.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
