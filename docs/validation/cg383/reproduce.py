"""Measure bounded cgroup-v2 file-cache reclaim before a one-slot launch.

Run this program *inside* a disposable delegated cgroup.  It creates its payload in a
temporary directory, so it never reads, deletes, or perturbs a garden cache.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


STAT_FIELDS = ("anon", "file", "inactive_file", "active_file", "slab_reclaimable")
EVENT_FIELDS = ("low", "high", "max", "oom", "oom_kill")


def cgroup_path() -> Path:
    """Return this process's unified cgroup-v2 directory."""
    relative = next(
        line.split("::", 1)[1]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    return Path("/sys/fs/cgroup") / relative.lstrip("/")


def fields(path: Path, names: tuple[str, ...]) -> dict[str, int]:
    values = dict(line.split(maxsplit=1) for line in path.read_text().splitlines())
    return {name: int(values.get(name, "0")) for name in names}


def psi_totals(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text().splitlines():
        kind, *values = line.split()
        total = next(value.split("=", 1)[1] for value in values if value.startswith("total="))
        result[kind] = int(total)
    return result


def sample(group: Path) -> dict[str, object]:
    return {
        "memory_current": int((group / "memory.current").read_text()),
        "memory_peak": int((group / "memory.peak").read_text()),
        "memory_stat": fields(group / "memory.stat", STAT_FIELDS),
        "memory_events": fields(group / "memory.events", EVENT_FIELDS),
        "memory_pressure_totals_usec": psi_totals(group / "memory.pressure"),
    }


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def populate_file_cache(path: Path, size_bytes: int) -> None:
    block = b"garden-cache-fixture\0" * (1024 * 64)
    with path.open("wb", buffering=0) as payload:
        remaining = size_bytes
        while remaining:
            chunk = block[:min(remaining, len(block))]
            payload.write(chunk)
            remaining -= len(chunk)
        payload.flush()
        os.fsync(payload.fileno())
    with path.open("rb", buffering=0) as payload:
        while payload.read(1024 * 1024):
            pass


def one_slot_high_water(slot_bytes: int) -> None:
    code = (
        "import sys, time\n"
        "payload = bytearray(int(sys.argv[1]))\n"
        "for offset in range(0, len(payload), 4096): payload[offset] = 1\n"
        "time.sleep(0.2)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(slot_bytes)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-mib", type=int, default=72)
    parser.add_argument("--reclaim-mib", type=int, default=64)
    parser.add_argument("--slot-mib", type=int, default=8)
    args = parser.parse_args()
    group = cgroup_path()
    cache_bytes = args.cache_mib * 1024 * 1024
    reclaim_bytes = args.reclaim_mib * 1024 * 1024
    slot_bytes = args.slot_mib * 1024 * 1024
    limits = {name: (group / name).read_text().strip() for name in ("memory.high", "memory.max")}
    before = sample(group)
    with tempfile.TemporaryDirectory(prefix="cg383-file-cache-") as directory:
        populate_file_cache(Path(directory) / "payload.bin", cache_bytes)
        populated = sample(group)
        reclaim_error: dict[str, object] | None = None
        try:
            (group / "memory.reclaim").write_text(str(reclaim_bytes))
        except OSError as exc:
            # Reclaim is explicitly best effort.  Keep an EAGAIN result in the evidence
            # and continue to the slot measurement rather than claiming capacity exists.
            reclaim_error = {"errno": exc.errno, "message": str(exc)}
        reclaimed = sample(group)
        one_slot_high_water(slot_bytes)
        launched = sample(group)
    result = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reproducer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version,
        "invocation": {"cache_mib": args.cache_mib, "reclaim_mib": args.reclaim_mib,
                       "slot_mib": args.slot_mib},
        "fixture": {"cgroup": str(group), "limits": limits, "cache_bytes": cache_bytes,
                    "reclaim_request_bytes": reclaim_bytes, "slot_bytes": slot_bytes},
        "reclaim_error": reclaim_error,
        "samples": {"before": before, "populated": populated, "reclaimed": reclaimed, "one_slot": launched},
        "deltas": {
            "cache_population": {"memory_current": populated["memory_current"] - before["memory_current"],
                                 "memory_stat": delta(before["memory_stat"], populated["memory_stat"])},
            "reclaim": {"memory_current": reclaimed["memory_current"] - populated["memory_current"],
                        "memory_stat": delta(populated["memory_stat"], reclaimed["memory_stat"]),
                        "memory_events": delta(populated["memory_events"], reclaimed["memory_events"]),
                        "memory_pressure_totals_usec": delta(populated["memory_pressure_totals_usec"], reclaimed["memory_pressure_totals_usec"])},
            "one_slot": {"memory_current": launched["memory_current"] - reclaimed["memory_current"],
                         "memory_peak": launched["memory_peak"],
                         "memory_events": delta(reclaimed["memory_events"], launched["memory_events"]),
                         "memory_pressure_totals_usec": delta(reclaimed["memory_pressure_totals_usec"], launched["memory_pressure_totals_usec"])},
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
