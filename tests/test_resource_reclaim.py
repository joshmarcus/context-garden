from __future__ import annotations

import json

from garden import resource_reclaim


def test_helper_refuses_replaced_cgroup_before_reclaim(monkeypatch, tmp_path):
    group = tmp_path / "group"
    original = tmp_path / "original"
    original.mkdir()
    for name, value in (("memory.current", "1"), ("memory.high", "100"),
                        ("memory.max", "200"), ("memory.reclaim", "")):
        (original / name).write_text(value)
    expected = original.stat()
    original.rename(group)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    for name, value in (("memory.current", "1"), ("memory.high", "100"),
                        ("memory.max", "200"), ("memory.reclaim", "")):
        (replacement / name).write_text(value)
    group.rename(original)
    replacement.rename(group)
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["resource_reclaim", "--cgroup", str(group), "--bytes", "10",
                                     "--timeout", "1", "--report", str(report), "--token", "test",
                                     "--device", str(expected.st_dev), "--inode", str(expected.st_ino)])

    assert resource_reclaim.main() == 0
    result = json.loads(report.read_text())
    assert result["status"] == "error"
    assert "identity changed before reclaim" in result["error"]
    assert (original / "memory.reclaim").read_text() == ""
    assert (group / "memory.reclaim").read_text() == ""
