"""Bounded accounting and conservative cleanup of Garden-owned local storage."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import stat
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


def path_identity(path: Path) -> dict[str, int] | None:
    """Return a stable, no-follow identity for restart reconciliation."""
    try:
        metadata = path.lstat()
    except OSError:
        return None
    return {"device": metadata.st_dev, "inode": metadata.st_ino, "mode": metadata.st_mode}


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect one component without following it, including Windows junctions."""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def owned_child(root: Path, child: Path) -> bool:
    """Prove child is below root with no symlink/reparse component in the path."""
    root = root.absolute()
    child = child.absolute()
    try:
        relative = child.relative_to(root)
    except ValueError:
        return False
    if child == root or _is_link_or_reparse(root):
        return False
    current = root
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse(current):
            return False
    return True


def owned_directory(root: Path, child: Path) -> bool:
    """Return whether an existing child is a real directory under an unlinkable path."""
    return owned_child(root, child) and child.is_dir()


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


def write_audit(garden_dir: Path, report: dict[str, object], *, keep: int = 20,
                destination: Path | None = None) -> Path:
    """Atomically and durably publish or update a sweep receipt."""
    audit_dir = garden_dir / "storage-cleanup"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    is_new = destination is None
    destination = destination or audit_dir / f"{stamp}.json"
    temporary = audit_dir / f".{stamp}.{os.getpid()}.tmp"
    with temporary.open("w") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    try:
        directory_fd = os.open(audit_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Directory fsync is unavailable on some supported filesystems/platforms. The
        # atomically replaced, fsynced file remains the strongest portable guarantee.
        pass
    if is_new:
        records = sorted(audit_dir.glob("*.json"))
        for obsolete in records[:-max(1, keep)]:
            try:
                obsolete.unlink()
            except OSError:
                pass
    return destination


def cleanup_home_caches(home: Path, root: Path, *, limit: int,
                        remove: Callable[[Path, Path], int] = remove_owned_tree,
                        on_pending: Callable[[Path, int], str] | None = None,
                        on_result: Callable[[dict[str, object]], None] | None = None,
                        ) -> list[dict[str, object]]:
    """Remove an allowlist of disposable caches, preserving credentials and model sessions."""
    results: list[dict[str, object]] = []
    for relative in DISPOSABLE_HOME_PATHS:
        if len(results) >= limit:
            break
        candidate = home / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        before = tree_bytes(candidate) if owned_directory(root, candidate) else 0
        operation_id = on_pending(candidate, before) if on_pending is not None else None
        try:
            reclaimed = remove(root, candidate)
            result = {"path": str(candidate), "outcome": "removed", "bytes_reclaimed": reclaimed}
        except (OSError, ValueError) as exc:
            result = {"path": str(candidate), "outcome": "failed", "bytes_reclaimed": 0,
                      "bytes_before": before, "error": str(exc)}
        if operation_id is not None:
            result["operation_id"] = operation_id
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results
