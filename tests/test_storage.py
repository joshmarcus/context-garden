from __future__ import annotations

import json
import subprocess

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


def test_wsl_probe_failure_does_not_expose_sensitive_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "_is_wsl", lambda: True)
    monkeypatch.setattr(storage.shutil, "which", lambda name: "/bin/powershell.exe")
    secret = r"C:\\Users\\private-user\\AppData\\Local\\Packages\\distro"

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"cannot access {secret}")

    monkeypatch.setattr(storage.subprocess, "run", run)
    backing = storage.measure_storage((tmp_path,), windows_backing_path=secret)[-1]

    assert backing.free_bytes is None
    assert "probe failed" in backing.error
    assert secret not in backing.error
    assert "private-user" not in backing.error


def test_native_probe_failure_does_not_expose_sensitive_path(monkeypatch, tmp_path):
    secret = tmp_path / "private-user" / "work"
    monkeypatch.setattr(storage, "_existing_parent", lambda path: secret)
    monkeypatch.setattr(storage.os, "stat", lambda path: (_ for _ in ()).throw(OSError(f"denied: {secret}")))

    volume = storage.measure_storage((secret,))[0]

    assert volume.free_bytes is None
    assert str(secret) not in volume.error
    assert volume.error == "measurement unavailable: filesystem path could not be inspected"
