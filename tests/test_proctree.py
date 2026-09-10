"""Platform-neutral process observation used by detached local runs."""

from __future__ import annotations

import os

from garden import proctree


def test_proc_snapshot_parses_parent_group_and_names_with_parentheses(tmp_path):
    process = tmp_path / "42"
    process.mkdir()
    (process / "stat").write_text("42 (worker (phase)) S 7 11 0 0 0\n")

    assert proctree._proc_processes(tmp_path) == [
        proctree.ProcessInfo(pid=42, ppid=7, pgid=11, state="S")
    ]


def test_ps_snapshot_uses_options_shared_by_bsd_and_procps(monkeypatch):
    monkeypatch.setattr(proctree, "_ps_lines", lambda *args: [
        " 42  7  11 Ss+", " 43 42  11 Z+", "not a process",
    ])

    assert proctree._ps_processes() == [
        proctree.ProcessInfo(pid=42, ppid=7, pgid=11, state="Ss+"),
        proctree.ProcessInfo(pid=43, ppid=42, pgid=11, state="Z+"),
    ]


def test_group_liveness_ignores_bsd_zombie_group(monkeypatch):
    monkeypatch.setattr(proctree, "_proc_root", lambda: None)
    monkeypatch.setattr(os, "killpg", lambda _pgid, _signal: None)
    monkeypatch.setattr(proctree, "_ps_group_live", lambda _pgid: False)

    assert not proctree.process_group_alive(42)


def test_descendants_uses_one_portable_process_snapshot(monkeypatch):
    monkeypatch.setattr(proctree, "_proc_root", lambda: None)
    monkeypatch.setattr(proctree, "_ps_children", lambda: {1: [2, 3], 2: [4]})

    assert proctree.descendants(1) == [2, 3, 4]
