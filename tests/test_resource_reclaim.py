from __future__ import annotations

import json

from garden import resource_reclaim


def test_helper_records_changed_cgroup_identity_as_error(monkeypatch, tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    for name, value in (("memory.current", "1"), ("memory.high", "100"),
                        ("memory.max", "200"), ("memory.reclaim", "")):
        (group / name).write_text(value)
    identities = iter([{"path": str(group), "device": 1, "inode": 1},
                       {"path": str(group), "device": 1, "inode": 2}])
    monkeypatch.setattr(resource_reclaim, "_identity", lambda path: next(identities))
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["resource_reclaim", "--cgroup", str(group), "--bytes", "10",
                                     "--timeout", "1", "--report", str(report), "--token", "test"])

    assert resource_reclaim.main() == 0
    result = json.loads(report.read_text())
    assert result["status"] == "error"
    assert "identity changed" in result["error"]
    assert result["headroom_before_bytes"] == 99
