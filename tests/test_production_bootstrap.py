"""Production bootstrap credential boundaries and vendor executable verification."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap():
    source = Path(__file__).resolve().parents[1] / "scripts/managed-worker-bootstrap"
    loader = importlib.machinery.SourceFileLoader("production_bootstrap_test", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def credentials():
    return {"codex_auth": {"tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}},
            "git_ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n",
            "git_repo": "example/project", "worker_token": "separate-worker-token"}


def test_secrets_leave_runtime_config_and_credentials_remain_refreshable(bootstrap, tmp_path):
    secret = credentials()
    files = bootstrap.production_files(secret)
    assert secret == {"worker_token": "separate-worker-token"}
    bootstrap.write_worker_files(files, os.getuid(), os.getgid(), home=tmp_path)
    auth = tmp_path / ".codex/auth.json"
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "synthetic-refresh"
    assert auth.stat().st_mode & 0o777 == 0o600
    assert auth.parent.stat().st_mode & 0o777 == 0o700
    # The worker must be able to retain refreshed tokens without access to the bootstrap secret.
    auth.write_text('{"tokens": {"refresh_token": "refreshed"}}')
    assert "refreshed" in auth.read_text()
    git = (tmp_path / ".gitconfig").read_text()
    assert "ssh://git@ssh.github.com:443/example/project.git" in git
    assert "synthetic-access" not in git
    assert "StrictHostKeyChecking=yes" in bootstrap.production_environment()


@pytest.mark.parametrize("field,value", [
    ("codex_auth", {}), ("git_ssh_private_key", "not a key"),
    ("git_repo", "example/project\n[credential] helper = unsafe"),
])
def test_malformed_credentials_are_rejected_before_install(bootstrap, field, value):
    secret = credentials()
    secret[field] = value
    with pytest.raises(ValueError):
        bootstrap.production_files(secret)


def archive(binary, *, symlink=False, missing="", manifest_version="0.153.4"):
    files = {
        "bin/codex": binary,
        "bin/codex-code-mode-host": b"#!/bin/sh\necho companion-ready\n",
        "codex-package.json": json.dumps({
            "layoutVersion": 1, "version": manifest_version, "target": "x86_64-unknown-linux-musl",
            "variant": "codex", "entrypoint": "bin/codex",
            "resourcesDir": "codex-resources", "pathDir": "codex-path",
        }).encode(),
        "codex-path/rg": b"#!/bin/sh\nexit 0\n",
        "codex-resources/bwrap": b"#!/bin/sh\nexit 0\n",
        "codex-resources/zsh/bin/zsh": b"#!/bin/sh\nexit 0\n",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in files.items():
            if name == missing:
                continue
            item = tarfile.TarInfo(name)
            if symlink and name == "bin/codex-code-mode-host":
                item.type = tarfile.SYMTYPE
                item.linkname = "/tmp/other"
                tar.addfile(item)
            else:
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))
    return data.getvalue()


def test_vendor_digest_precedes_any_executable_install(bootstrap, monkeypatch, tmp_path):
    data = archive(b"#!/bin/sh\necho codex-cli 0.153.4\n")
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    output = tmp_path / "codex"
    package = tmp_path / "package"
    with pytest.raises(ValueError, match="checksum"):
        bootstrap.install_codex(output, package)
    assert not output.exists()
    assert not package.exists()
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    previous_umask = os.umask(0o077)
    try:
        bootstrap.install_codex(output, package)
    finally:
        os.umask(previous_umask)
    assert os.access(output, os.X_OK)
    assert output.resolve() == package / "bin/codex"
    companion = output.with_name("codex-code-mode-host")
    assert subprocess.check_output([str(companion)], text=True).strip() == "companion-ready"
    assert json.loads((package / "codex-package.json").read_text())["resourcesDir"] == "codex-resources"
    # Root runs bootstrap with a private umask; all vendor directories must still be
    # traversable by the separate unprivileged worker.
    assert all(p.stat().st_mode & 0o444 == 0o444 for p in package.rglob("*"))
    assert all(p.stat().st_mode & 0o111 == 0o111 for p in package.rglob("*") if p.is_dir())
    assert package.stat().st_mode & 0o555 == 0o555


def test_archive_links_are_not_installed_as_executables(bootstrap, monkeypatch, tmp_path):
    data = archive(b"", symlink=True)
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError, match="regular executable"):
        bootstrap.install_codex(tmp_path / "codex", tmp_path / "package")
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize("missing", ["bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap"])
def test_incomplete_vendor_package_is_rejected_before_install(bootstrap, monkeypatch, tmp_path, missing):
    data = archive(b"#!/bin/sh\necho codex-cli 0.153.4\n", missing=missing)
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError, match="incomplete"):
        bootstrap.install_codex(tmp_path / "codex", tmp_path / "package")
    assert list(tmp_path.iterdir()) == []


def test_wrong_package_manifest_is_rejected_before_install(bootstrap, monkeypatch, tmp_path):
    data = archive(b"#!/bin/sh\necho codex-cli 0.153.4\n", manifest_version="different")
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError, match="manifest"):
        bootstrap.install_codex(tmp_path / "codex", tmp_path / "package")
    assert list(tmp_path.iterdir()) == []


def test_clean_image_bootstrap_excludes_browser_installation_by_default():
    source = (Path(__file__).resolve().parents[1] / "scripts/managed-worker-bootstrap").read_text()
    assert source.index("verify_absolute_deadline(declaration)") < source.index('command("apt-get", "update"')

    for package in ("python3-pip", "gh", "make"):
        assert f'"{package}"' in source
    for browser_item in ("libnss3", "libgbm1", "libasound2", '"-m", "playwright", "install", "chromium"'):
        assert browser_item not in source
    assert '"runuser", "-u", "garden-worker"' in source
    assert "p.chromium.launch(headless=True)" not in source
    assert '"git", "ls-remote"' in source
    assert '"repository_access": {"ok": True' in source
    assert '"ci_provider_read": ci_read' in source
    assert "PLAYWRIGHT_BROWSERS_PATH" not in source


def test_installed_source_attestation_requires_exact_direct_url_commit(bootstrap):
    head = "a" * 40
    def output(command, **kwargs):
        assert command[:2] == ["/venv/python", "-c"]
        assert kwargs == {"text": True, "timeout": 30}
        return json.dumps({"distribution": "context-garden", "version": "1.2.3",
                           "commit": head, "vcs": "git"})

    assert bootstrap.installed_source_attestation("/venv/python", head, check_output=output) == {
        "ok": True, "distribution": "context-garden", "version": "1.2.3",
        "direct_url_commit": head,
    }
    with pytest.raises(ValueError, match="source provenance"):
        bootstrap.installed_source_attestation("/venv/python", "b" * 40, check_output=output)


def test_bootstrap_requires_matching_independently_armed_absolute_deadline(bootstrap, tmp_path):
    unit = tmp_path / "garden-host-deadline.timer"
    unit.write_text("[Timer]\nOnCalendar=2026-09-10 23:36:42 UTC\nPersistent=true\n")
    calls = []
    def output(command, **kwargs):
        calls.append((command, kwargs))
        return "enabled\n" if command[1] == "is-enabled" else "active\n"
    declaration = {"deadline_utc": "2026-09-10T23:36:42Z"}
    assert bootstrap.verify_absolute_deadline(
        declaration, unit=unit, check_output=output) == declaration["deadline_utc"]
    assert [call[0][1] for call in calls] == ["is-enabled", "is-active"]
    with pytest.raises(ValueError, match="does not match"):
        bootstrap.verify_absolute_deadline(
            {"deadline_utc": "2026-09-10T23:36:43Z"}, unit=unit, check_output=output)
    assert bootstrap.verify_absolute_deadline(
        {"deadline_utc": "2026-09-10T23:36:42+00:00"}, unit=unit,
        check_output=output) == "2026-09-10T23:36:42+00:00"


def test_bootstrap_service_caps_come_from_immutable_profile_declaration(bootstrap):
    assert bootstrap.declared_resource_caps(
        {"cpu": 3, "memory_mib": 12288, "disk_gib": 40}) == {
            "cpu": 3, "memory_mib": 12288, "disk_gib": 40}
    for invalid in (0, True, 1.5):
        with pytest.raises(ValueError, match="immutable positive integer"):
            bootstrap.declared_resource_caps(
                {"cpu": invalid, "memory_mib": 12288, "disk_gib": 40})
    source = (Path(__file__).resolve().parents[1] / "scripts/managed-worker-bootstrap").read_text()
    assert "MemoryMax={capacity['memory_mib']}M" in source
    assert "CPUQuota={capacity['cpu'] * 100}%" in source
    assert "MemoryMax=12G" not in source and "CPUQuota=300%" not in source


def test_bootstrap_manifest_binds_executed_bytes_and_source_identity(bootstrap, tmp_path):
    script = tmp_path / "bootstrap"
    script.write_bytes(b"pinned bootstrap bytes")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    declaration = {"bootstrap_sha256": digest, "profile_version": "profile-v3",
                   "bootstrap_version": "bootstrap-v5"}
    installed = {"distribution": "context-garden", "direct_url_commit": "c" * 40}
    result = bootstrap.bootstrap_manifest_attestation(
        declaration, "c" * 40, installed, script=script)
    assert result == {"ok": True, "source_head": "c" * 40,
                      "profile_version": "profile-v3", "bootstrap_version": "bootstrap-v5",
                      "bootstrap_sha256": digest, "installed_distribution": "context-garden",
                      "direct_url_commit": "c" * 40}
    declaration["bootstrap_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact digest"):
        bootstrap.bootstrap_manifest_attestation(declaration, "c" * 40, installed, script=script)


def test_ci_read_probe_is_bounded_and_never_returns_token(bootstrap):
    token = "private-scoped-ci-token"
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{"total_count": 0, "check_runs": []}'
    def open_request(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        return Response()

    result = bootstrap.github_ci_read_attestation(
        "example/project", "d" * 40, token, opener=open_request)
    assert result == {"ok": True, "provider": "github", "repository": "example/project",
                      "source_head": "d" * 40, "authenticated": True}
    assert captured["timeout"] == 30
    assert captured["url"].endswith("/commits/" + "d" * 40 + "/check-runs?per_page=1")
    assert captured["headers"]["Authorization"] == "Bearer " + token
    assert token not in json.dumps(result)


def test_ci_read_probe_rejects_non_provider_payload(bootstrap):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{"message": "rate limited"}'
    with pytest.raises(ValueError, match="invalid response"):
        bootstrap.github_ci_read_attestation(
            "example/project", "e" * 40, opener=lambda request, timeout: Response())
