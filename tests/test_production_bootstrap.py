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


def test_clean_image_bootstrap_includes_full_test_environment():
    source = (Path(__file__).resolve().parents[1] / "scripts/managed-worker-bootstrap").read_text()

    for package in ("python3-pip", "gh", "make", "libnss3", "libgbm1", "libasound2"):
        assert f'"{package}"' in source
    assert '"-m", "playwright", "install", "chromium"' in source
    assert '"runuser", "-u", "garden-worker"' in source
    assert "p.chromium.launch(headless=True)" in source
    assert '"git", "ls-remote"' in source
    assert '"repository_ci": True' in source
    assert "Environment=PLAYWRIGHT_BROWSERS_PATH=/var/lib/garden-worker/browsers" in source
