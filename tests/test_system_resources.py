from __future__ import annotations

import subprocess

from garden import system_resources


def test_linux_memory_reads_procfs_without_platform_commands(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8192 kB\nMemAvailable: 3072 kB\n")

    assert system_resources.memory_bytes(meminfo) == (3072 * 1024, 8192 * 1024)


def test_darwin_memory_uses_vm_stat_and_sysctl(tmp_path, monkeypatch):
    missing_procfs = tmp_path / "missing"
    monkeypatch.setattr(system_resources.sys, "platform", "darwin")

    def run(command, **_kwargs):
        if command[0].endswith("vm_stat"):
            return subprocess.CompletedProcess(command, 0, (
                "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                "Pages free: 10.\nPages inactive: 20.\nPages speculative: 5.\n"
                "Pages active: 100.\n"
            ), "")
        return subprocess.CompletedProcess(command, 0, "1048576\n", "")

    monkeypatch.setattr(system_resources.subprocess, "run", run)

    assert system_resources.memory_bytes(missing_procfs) == (35 * 4096, 1048576)


def test_swap_usage_supports_linux_and_darwin(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("SwapTotal: 8192 kB\nSwapFree: 3072 kB\n")
    assert system_resources.swap_used_bytes(meminfo) == 5120 * 1024

    monkeypatch.setattr(system_resources.sys, "platform", "darwin")
    monkeypatch.setattr(system_resources.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, "total = 2.00G used = 1.25G free = 0.75G", ""))
    assert system_resources.swap_used_bytes(tmp_path / "missing") == int(1.25 * 1024**3)
