"""Production bootstrap credential boundaries and vendor executable verification."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
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


def archive(binary, *, symlink=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        item = tarfile.TarInfo("codex-x86_64-unknown-linux-musl")
        if symlink:
            item.type = tarfile.SYMTYPE
            item.linkname = "/tmp/other"
            tar.addfile(item)
        else:
            item.size = len(binary)
            tar.addfile(item, io.BytesIO(binary))
    return data.getvalue()


def test_vendor_digest_precedes_any_executable_install(bootstrap, monkeypatch, tmp_path):
    data = archive(b"#!/bin/sh\necho codex-cli 0.153.4\n")
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    output = tmp_path / "codex"
    with pytest.raises(ValueError, match="checksum"):
        bootstrap.install_codex(output)
    assert not output.exists()
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    bootstrap.install_codex(output)
    assert os.access(output, os.X_OK)


def test_archive_links_are_not_installed_as_executables(bootstrap, monkeypatch, tmp_path):
    data = archive(b"", symlink=True)
    monkeypatch.setattr(bootstrap, "fetch", lambda url: data)
    monkeypatch.setattr(bootstrap, "CODEX_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError, match="regular executable"):
        bootstrap.install_codex(tmp_path / "codex")
