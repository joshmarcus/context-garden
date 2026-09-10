"""Bounded accounting and conservative cleanup of Garden-owned local storage."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

DISPOSABLE_HOME_PATHS = (
    ".cache/pip", ".cache/uv", ".cache/ms-playwright", ".cache/playwright",
    ".npm/_cacache", ".pnpm-store", ".yarn/cache",
)


@dataclass(frozen=True)
class StorageItem:
    path: str
    category: str
    owner: str
    bytes: int
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def tree_bytes(path: Path) -> int:
    """Return allocated bytes without following symlinks or crossing directory links."""
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            continue
    return total


def owned_child(root: Path, child: Path) -> bool:
    """Lexically prove child is below root; resolving would follow attacker-made links."""
    try:
        child.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return child != root and not child.is_symlink()


def remove_owned_tree(root: Path, path: Path) -> int:
    """Remove one non-link tree below an owned root and return its measured bytes."""
    if not owned_child(root, path):
        raise ValueError(f"refusing path outside owned root or symlink: {path}")
    size = tree_bytes(path)
    shutil.rmtree(path)
    return size


def space_status(path: Path, *, probe_host: bool = True) -> dict[str, object]:
    """Report the writable guest filesystem and, when available, Windows host volume."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    result: dict[str, object] = {"path": str(probe), "guest_free_bytes": None,
                                "host_free_bytes": None, "host_reason": "not running under WSL"}
    try:
        result["guest_free_bytes"] = shutil.disk_usage(probe).free
    except OSError as exc:
        result["guest_reason"] = str(exc)
    if not probe_host:
        result["host_reason"] = "not measured during incremental scheduler sweep"
        return result
    if "microsoft" not in os.uname().release.lower():
        return result
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        result["host_reason"] = "PowerShell capability unavailable"
        return result
    # WSL's virtual disk backing file may live on a different volume.  The system drive is
    # useful pressure evidence, but is explicitly labelled rather than claimed as the VHDX.
    script = "[int64](Get-PSDrive -Name $env:SystemDrive.TrimEnd(':')).Free"
    try:
        proc = subprocess.run([powershell, "-NoProfile", "-Command", script], capture_output=True,
                              text=True, timeout=5, check=False)
        if proc.returncode == 0:
            result["host_free_bytes"] = int(proc.stdout.strip())
            result["host_reason"] = "Windows system-volume free space; VHDX backing-volume identity unavailable"
        else:
            result["host_reason"] = "Windows host probe failed"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        result["host_reason"] = "Windows host probe unavailable"
    return result


def write_audit(garden_dir: Path, report: dict[str, object], *, keep: int = 20) -> Path:
    """Durably publish each sweep result without replacing the previous evidence first."""
    audit_dir = garden_dir / "storage-cleanup"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = audit_dir / f"{stamp}.json"
    temporary = audit_dir / f".{stamp}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, destination)
    records = sorted(audit_dir.glob("*.json"))
    for obsolete in records[:-max(1, keep)]:
        try:
            obsolete.unlink()
        except OSError:
            pass
    return destination


def cleanup_home_caches(home: Path, root: Path, *, limit: int,
                        remove: Callable[[Path, Path], int] = remove_owned_tree) -> list[dict[str, object]]:
    """Remove an allowlist of disposable caches, preserving credentials and model sessions."""
    results: list[dict[str, object]] = []
    for relative in DISPOSABLE_HOME_PATHS:
        if len(results) >= limit:
            break
        candidate = home / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        before = tree_bytes(candidate) if candidate.is_dir() and not candidate.is_symlink() else 0
        try:
            reclaimed = remove(root, candidate)
            results.append({"path": str(candidate), "outcome": "removed", "bytes_reclaimed": reclaimed})
        except (OSError, ValueError) as exc:
            results.append({"path": str(candidate), "outcome": "failed", "bytes_reclaimed": 0,
                            "bytes_before": before, "error": str(exc)})
    return results
