from __future__ import annotations

import json

from garden import storage


def test_native_measurements_deduplicate_filesystems(tmp_path):
    volumes = storage.measure_storage((tmp_path, tmp_path / "not-created"))
    native = [volume for volume in volumes if volume.key.startswith("device:")]
    assert len(native) == 1
    assert native[0].free_bytes is not None


def test_wsl_measures_discovered_backing_volume_without_assuming_drive(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(storage, "_is_wsl", lambda: True)
    monkeypatch.setattr(storage.shutil, "which", lambda name: "/bin/powershell.exe")

    def run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"free": 123}), "stderr": ""})()

    monkeypatch.setattr(storage.subprocess, "run", run)
    backing = storage.measure_storage((tmp_path,))[-1]
    assert backing.label == "Windows backing volume" and backing.free_bytes == 123
    assert "DistributionName" in calls[0][-1]
    assert "C:" not in calls[0][-1]


def test_wsl_explicit_alternate_backing_path_is_quoted(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "_is_wsl", lambda: True)
    monkeypatch.setattr(storage.shutil, "which", lambda name: "/bin/powershell.exe")
    commands = []

    def run(command, **kwargs):
        commands.append(command[-1])
        return type("Result", (), {"returncode": 0, "stdout": '{"free":456}', "stderr": ""})()

    monkeypatch.setattr(storage.subprocess, "run", run)
    assert storage.measure_storage((tmp_path,), windows_backing_path="D:\\WSL's data")[-1].free_bytes == 456
    assert "D:\\WSL''s data" in commands[0]


def test_wsl_inaccessible_probe_is_unknown_not_fabricated(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "_is_wsl", lambda: True)
    monkeypatch.setattr(storage.shutil, "which", lambda name: None)
    backing = storage.measure_storage((tmp_path,))[-1]
    assert backing.free_bytes is None
    assert "unavailable" in backing.error
