"""Bounded, read-only health probes for statically configured SSH workers.

A pull worker reports its own presence, so the fleet projection knows when it last spoke.  A
static `ssh.hosts` entry has no agent: nothing reports it, and the projection can only say
"unknown" about it until something looks.  This module looks — on a configured cadence, with
every configured host probed at once, in a process the scheduler starts and never waits for —
and caches one reading per host that the page, the API and the CLI only read.

Two properties are deliberate.  The probe cannot change a worker: it runs `command -v` and
`[ -d ]`, never a git write, `git status`, a formatter or a build, so probing can never
reformat or relocate a checkout a run depends on.  And it never runs inside a request or a
tick: :func:`probe_readings` reads the cache and nothing else, so an unreachable host delays
neither an HTTP response nor a scheduler pass.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Printed last by a probe that ran to completion, so a truncated or hijacked session reads as
# a failure rather than a clean host.
COMPLETE = "garden-probe-complete"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 15
MAX_CONCURRENT = 8
REASON_CHARS = 300


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _seconds(value: Any, default: float) -> float:
    """A positive number, or the default: a probe cadence is never a place to fail a tick."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


@dataclass(frozen=True)
class ProbeSettings:
    """The `ssh` block's probe contract, resolved once per pass."""

    interval_seconds: float
    timeout_seconds: float
    ssh_bin: str
    options: list[str]
    tools: tuple[str, ...]

    @property
    def stale_after_seconds(self) -> float:
        """One missed pass is ordinary; two mean the reading no longer describes the host."""
        return self.interval_seconds * 2


def _block(config: Any) -> dict[str, Any]:
    block = config.get("ssh")
    return dict(block) if isinstance(block, dict) else {}


def probe_settings(config: Any) -> ProbeSettings:
    block = _block(config)
    return ProbeSettings(
        interval_seconds=_seconds(block.get("probe_interval_seconds"), DEFAULT_INTERVAL_SECONDS),
        timeout_seconds=_seconds(block.get("probe_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
        ssh_bin=str(block.get("ssh_bin") or "ssh"),
        options=[str(option) for option in (block.get("options") or ["-o", "BatchMode=yes"])],
        # What the SSH runner needs before it can start a durable session at all.
        tools=("git", "tmux", str(block.get("python") or "python3")),
    )


def probe_targets(config: Any) -> list[dict[str, Any]]:
    """The configured hosts a probe can reach: a garden-local name and an SSH destination."""
    return [dict(row) for row in (config.get("ssh.hosts") or [])
            if row.get("name") and row.get("host")]


class SSHProbeStore:
    """The durable cache: one reading per configured host name, plus the pass in flight."""

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "ssh-probes.json"

    def document(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def hosts(self) -> dict[str, dict[str, Any]]:
        hosts = self.document().get("hosts")
        return {str(name): dict(row) for name, row in hosts.items() if isinstance(row, dict)} \
            if isinstance(hosts, dict) else {}

    def record(self, results: dict[str, dict[str, Any]], *, now: dt.datetime | None = None) -> None:
        """Merge one pass's results, preserving each host's last successful contact."""
        stamp = (now or _now()).isoformat()
        with self._edit() as document:
            hosts = {str(name): dict(row)
                     for name, row in (document.get("hosts") or {}).items()
                     if isinstance(row, dict)}
            for name, result in results.items():
                previous = hosts.get(name, {})
                hosts[name] = {
                    **result, "checked_at": stamp,
                    "last_success_at": stamp if result.get("ok")
                    else str(previous.get("last_success_at") or ""),
                }
            document["hosts"] = hosts
            document["completed_at"] = stamp

    def request(self, *, now: dt.datetime | None = None) -> None:
        """Mark a pass as started, so a slow one is not started again every tick."""
        with self._edit() as document:
            document["requested_at"] = (now or _now()).isoformat()

    @contextmanager
    def _edit(self) -> Iterator[dict[str, Any]]:
        """Read, modify and atomically rewrite the cache under an exclusive lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            document = self.document()
            yield document
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
            temporary.replace(self.path)


def probe_readings(config: Any, *, now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    """The cached reading per host, with staleness derived from the configured cadence.

    This is the read every surface uses.  It opens no connection and starts no process, so it
    is safe inside HTTP rendering and inside a scheduler pass.
    """
    settings = probe_settings(config)
    moment = now or _now()
    readings: dict[str, dict[str, Any]] = {}
    for name, row in SSHProbeStore(Path(config.garden_dir)).hosts().items():
        checked = _parse(str(row.get("checked_at") or ""))
        age = (moment - checked).total_seconds() if checked else None
        readings[name] = {
            "ok": bool(row.get("ok")),
            "latency_ms": int(row.get("latency_ms") or 0),
            "checked_at": str(row.get("checked_at") or "") or None,
            "last_success_at": str(row.get("last_success_at") or "") or None,
            "reason": str(row.get("reason") or ""),
            "age_seconds": int(age) if age is not None else None,
            "stale": age is None or age > settings.stale_after_seconds,
        }
    return readings


def probe_lines(config: Any, *, now: dt.datetime | None = None) -> list[str]:
    """One operator-readable line per statically configured host, from the cache only.

    Doctor prints these beside the managed pool's reading, so both kinds of worker are
    accounted for in the same place.  A host is named by its garden-local name; the
    connection target stays where it was configured.
    """
    readings = probe_readings(config, now=now)
    lines: list[str] = []
    for target in probe_targets(config):
        name = str(target["name"])
        reading = readings.get(name)
        if reading is None:
            lines.append(f"static host {name}: no probe reading yet")
            continue
        detail = [f"{reading['latency_ms']} ms", f"checked {reading['checked_at']}"]
        if reading["stale"]:
            detail.append("stale reading")
        detail.append(f"last success {reading['last_success_at'] or 'never'}")
        if reading["reason"]:
            detail.append(reading["reason"])
        state = "reachable" if reading["ok"] else "unreachable"
        lines.append(f"static host {name}: {state} · " + " · ".join(detail))
    return lines


def unreachable_hosts(config: Any, *, now: dt.datetime | None = None) -> list[str]:
    """Hosts whose latest reading is both fresh and a failure — a real, current problem."""
    return sorted(name for name, reading in probe_readings(config, now=now).items()
                  if not reading["ok"] and not reading["stale"])


def probes_due(config: Any, *, now: dt.datetime | None = None) -> bool:
    """Whether a new pass is owed: a host never probed, or one probed a cadence ago."""
    targets = probe_targets(config)
    if not targets:
        return False
    settings = probe_settings(config)
    moment = now or _now()
    document = SSHProbeStore(Path(config.garden_dir)).document()
    requested = _parse(str(document.get("requested_at") or ""))
    if requested and (moment - requested).total_seconds() < settings.timeout_seconds * 2:
        return False  # a pass is still in flight; it holds the lock and will write the cache
    hosts = document.get("hosts") if isinstance(document.get("hosts"), dict) else {}
    for target in targets:
        checked = _parse(str((hosts.get(str(target["name"])) or {}).get("checked_at") or ""))
        if checked is None or (moment - checked).total_seconds() >= settings.interval_seconds:
            return True
    return False


def _script(settings: ProbeSettings, repos: list[str]) -> str:
    """A read-only shell probe: nothing here writes, formats, or fetches."""
    lines = [f'command -v {shlex.quote(tool)} >/dev/null 2>&1 || echo missing tool: {tool}'
             for tool in settings.tools]
    for repo in repos:
        path = shlex.quote(repo)
        lines.append(f'if [ ! -d {path} ]; then echo missing checkout: {path}; '
                     f'elif [ ! -e {path}/.git ]; then echo missing git metadata: {path}; fi')
    lines.append(f"echo {COMPLETE}")
    return "\n".join(lines) + "\n"


def _diagnosis(completed: subprocess.CompletedProcess[str]) -> str:
    if completed.returncode:
        tail = [line for line in (completed.stderr or "").strip().splitlines() if line.strip()]
        return (f"ssh exited {completed.returncode}: "
                + (tail[-1].strip() if tail else "no transport diagnostic"))
    reported = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if COMPLETE not in reported:
        return "probe did not complete on the host"
    return "; ".join(line for line in reported if line != COMPLETE)


def probe_host(host: dict[str, Any], settings: ProbeSettings, *, run: Any = None) -> dict[str, Any]:
    """One bounded probe of one host.  Never raises: a failure is a reading, not an error."""
    execute = run or subprocess.run
    repos = sorted({str(path) for path in (host.get("repos") or {}).values() if str(path).strip()})
    command = [settings.ssh_bin, *settings.options, str(host["host"]), "sh", "-s"]
    started = time.monotonic()
    try:
        completed = execute(command, input=_script(settings, repos), capture_output=True,
                           text=True, timeout=settings.timeout_seconds)
    except subprocess.TimeoutExpired:
        reason = f"probe timed out after {settings.timeout_seconds:g}s"
    except (OSError, subprocess.SubprocessError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        # An unexpected transport failure may carry request state; keep the type only.
        reason = f"probe failed ({type(exc).__name__})"
    else:
        reason = _diagnosis(completed)
    return {"ok": not reason, "latency_ms": int((time.monotonic() - started) * 1000),
            "reason": reason[:REASON_CHARS]}


def run_probe_pass(config: Any, *, now: dt.datetime | None = None,
                   run: Any = None) -> dict[str, dict[str, Any]]:
    """Probe every configured host concurrently and cache the results.

    The controller never calls this in its own process; :func:`probe_readings` is the read
    side.  Concurrency means one slow host costs the pass its own timeout, not the sum.
    """
    targets = probe_targets(config)
    if not targets:
        return {}
    settings = probe_settings(config)
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(targets))) as pool:
        results = dict(zip(
            [str(target["name"]) for target in targets],
            pool.map(lambda target: probe_host(target, settings, run=run), targets),
            strict=True,
        ))
    SSHProbeStore(Path(config.garden_dir)).record(results, now=now)
    return results


def main(argv: list[str] | None = None) -> int:
    """Run one pass for the garden at ``argv[0]``, or exit if another pass owns the cadence."""
    from .config import Config

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m garden.ssh_probe <garden-root>", file=sys.stderr)
        return 2
    config = Config.load(Path(arguments[0]).resolve())
    garden_dir = Path(config.garden_dir)
    garden_dir.mkdir(parents=True, exist_ok=True)
    with (garden_dir / "ssh-probe.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        run_probe_pass(config)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module by the scheduler
    raise SystemExit(main())
