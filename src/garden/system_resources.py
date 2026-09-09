"""Small host-resource probes with explicit Linux and Darwin implementations."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _linux_memory_bytes(path: Path) -> tuple[int | None, int | None]:
    try:
        values = {
            key: int(value.split()[0]) * 1024
            for line in path.read_text().splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
    except (OSError, ValueError, IndexError):
        return None, None
    return values.get("MemAvailable"), values.get("MemTotal")


def _darwin_memory_bytes() -> tuple[int | None, int | None]:
    """Available and total bytes from stable macOS command-line kernel interfaces."""
    try:
        vm = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    page_match = re.search(r"page size of (\d+) bytes", vm.stdout)
    pages: dict[str, int] = {}
    if vm.returncode == 0 and page_match:
        for line in vm.stdout.splitlines()[1:]:
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            try:
                pages[name.strip()] = int(raw.strip().rstrip("."))
            except ValueError:
                continue
    available = None
    if page_match and pages:
        # Inactive and speculative pages are reclaimable without swapping active memory.
        available_pages = sum(pages.get(name, 0) for name in (
            "Pages free", "Pages inactive", "Pages speculative",
        ))
        available = available_pages * int(page_match.group(1))

    total = None
    try:
        physical = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if physical.returncode == 0:
            total = int(physical.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return available, total


def memory_bytes(path: Path = Path("/proc/meminfo")) -> tuple[int | None, int | None]:
    """Return available and total host memory without assuming procfs exists."""
    available, total = _linux_memory_bytes(path)
    if available is not None or total is not None:
        return available, total
    if sys.platform == "darwin":
        return _darwin_memory_bytes()
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
    except (OSError, ValueError):
        total = None
    return None, total


def swap_used_bytes(path: Path = Path("/proc/meminfo")) -> int | None:
    """Return used swap on Linux or Darwin, or None when the host cannot report it."""
    try:
        values = {
            key: int(value.split()[0]) * 1024
            for line in path.read_text().splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        if "SwapTotal" in values and "SwapFree" in values:
            return max(0, values["SwapTotal"] - values["SwapFree"])
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform != "darwin":
        return None
    try:
        swap = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\bused\s*=\s*([0-9.]+)([KMG])\b", swap.stdout)
    if swap.returncode != 0 or not match:
        return None
    scale = {"K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2)]
    return int(float(match.group(1)) * scale)
