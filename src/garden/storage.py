"""Physical storage discovery for local admission.

The scheduler consumes this module through :func:`measure_storage`.  External operator
tooling may use the same function, passing the paths it intends to materialize and an
optional Windows backing path for WSL.  Measurements fail closed: an unknown required
volume is returned with an error, never with invented capacity.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageVolume:
    key: str
    label: str
    free_bytes: int | None
    error: str = ""


class StorageAdmissionError(RuntimeError):
    """A required physical volume cannot safely admit materialization."""


def _existing_parent(path: Path) -> Path:
    probe = path.resolve(strict=False)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _native_volumes(paths: tuple[Path, ...]) -> list[StorageVolume]:
    volumes: dict[int, StorageVolume] = {}
    for path in paths:
        try:
            probe = _existing_parent(path)
            stat = os.stat(probe)
            usage = shutil.disk_usage(probe)
            volumes.setdefault(stat.st_dev, StorageVolume(f"device:{stat.st_dev}", "local filesystem", usage.free))
        except OSError as exc:
            key = f"path:{len(volumes)}"
            volumes[key] = StorageVolume(key, "local filesystem", None, f"measurement unavailable: {exc}")
    return list(volumes.values())


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower() or bool(os.environ.get("WSL_DISTRO_NAME"))


def _windows_backing_free(path: str) -> StorageVolume:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return StorageVolume("windows-backing", "Windows backing volume", None,
                             "PowerShell is unavailable; configure resources.windows_backing_path and ensure interop is enabled")
    # An explicit path can name any drive.  Otherwise resolve this distro's BasePath from
    # HKCU\...\Lxss, avoiding assumptions about C:, the Windows user, or package layout.
    escaped = path.replace("'", "''")
    script = (
        f"$p='{escaped}'; "
        "if (-not $p) {$n=$env:WSL_DISTRO_NAME; $k=Get-ChildItem HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss | "
        "Where-Object {(Get-ItemProperty $_.PSPath).DistributionName -eq $n} | Select-Object -First 1; "
        "if ($k) {$p=(Get-ItemProperty $k.PSPath).BasePath}}; "
        "if (-not $p) {throw 'WSL backing volume could not be resolved'}; "
        "$i=Get-Item -LiteralPath $p -ErrorAction Stop; $d=$i.PSDrive; "
        "@{free=[int64]$d.Free}|ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode:
            raise OSError((proc.stderr or proc.stdout or "PowerShell probe failed").strip())
        free = int(json.loads(proc.stdout)["free"])
        return StorageVolume("windows-backing", "Windows backing volume", free)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return StorageVolume("windows-backing", "Windows backing volume", None,
                             f"measurement unavailable: {exc}")


def measure_storage(paths: tuple[Path, ...], *, windows_backing_path: str = "") -> tuple[StorageVolume, ...]:
    """Measure each distinct native filesystem and WSL's physical Windows backing volume."""
    volumes = _native_volumes(paths)
    if _is_wsl():
        volumes.append(_windows_backing_free(windows_backing_path))
    return tuple(volumes)


def require_storage(paths: tuple[Path, ...], *, reserve_bytes: int, required_bytes: int = 0,
                    windows_backing_path: str = "", operation: str = "local materialization") -> tuple[StorageVolume, ...]:
    """Measure fresh capacity and reject an operation that would cross its reserve."""
    volumes = measure_storage(paths, windows_backing_path=windows_backing_path)
    if not reserve_bytes:
        return volumes
    reasons = []
    for volume in volumes:
        if volume.free_bytes is None:
            reasons.append(f"{volume.label} {volume.error}")
        elif volume.free_bytes - required_bytes < reserve_bytes:
            reasons.append(
                f"{volume.label} has {volume.free_bytes} free bytes; reserve {reserve_bytes} bytes "
                f"plus {required_bytes} required bytes is required"
            )
    if reasons:
        raise StorageAdmissionError(f"{operation} blocked: {'; '.join(reasons)}")
    return volumes
