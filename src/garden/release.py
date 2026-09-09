"""Local release identity and candidate validation.

This module deliberately does not call GitHub.  A release candidate carries the CI links
and artifact digest manifest that a human has reviewed; validation checks that those claims
agree with the checkout before someone creates a draft or prerelease on the forge.
"""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .upgrade import DEFAULT_PACKAGE, installed_commit


@dataclass(frozen=True)
class InstalledIdentity:
    """The installed distribution version and its VCS source, if pip recorded one."""

    version: str
    source: str | None


def installed_identity(package: str = DEFAULT_PACKAGE) -> InstalledIdentity:
    """Return installed metadata without consulting a remote package index or forge."""
    try:
        from importlib.metadata import version

        package_version = version(package)
    except Exception:  # noqa: BLE001 - editable/source execution may lack distribution metadata
        from . import __version__

        package_version = __version__
    return InstalledIdentity(version=package_version, source=installed_commit(package))


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _required(data: dict[str, Any], field: str) -> str:
    value = str(data.get(field) or "").strip()
    if not value:
        raise ValueError(f"release manifest requires {field}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_candidate(manifest_path: Path, *, root: Path, github_host: str = "github.com",
                       repository: str = "") -> list[str]:
    """Validate a local candidate manifest and return its human-readable checks.

    The command has no publication side effect.  It accepts a GitHub Enterprise host and
    repository explicitly so CI evidence is not accidentally validated against github.com.
    """
    manifest_path = manifest_path.resolve()
    root = root.resolve()
    data = _load_mapping(manifest_path)
    version = _required(data, "version")
    tag = _required(data, "tag")
    commit = _required(data, "commit")
    release_kind = _required(data, "kind")
    if release_kind not in {"draft", "prerelease"}:
        raise ValueError("release manifest kind must be draft or prerelease")
    if tag != f"v{version}":
        raise ValueError(f"tag {tag!r} must equal v{version}")

    project = tomllib.loads((root / "pyproject.toml").read_text())
    project_version = str(project.get("project", {}).get("version") or "")
    if project_version != version:
        raise ValueError(f"pyproject version {project_version!r} does not match {version!r}")
    resolved_commit = _git(root, "rev-parse", f"{commit}^{{commit}}")
    if _git(root, "cat-file", "-t", tag) != "tag":
        raise ValueError(f"tag {tag} must be annotated")
    tagged_commit = _git(root, "rev-parse", f"{tag}^{{commit}}")
    if resolved_commit != tagged_commit:
        raise ValueError(f"tag {tag} points to {tagged_commit}, not {resolved_commit}")

    notes = (manifest_path.parent / _required(data, "notes")).resolve()
    if not notes.is_file() or not notes.read_text().strip():
        raise ValueError("release notes must name a non-empty file")

    configured_repository = repository or str(data.get("repository") or "")
    if not configured_repository or "/" not in configured_repository:
        raise ValueError("release manifest requires repository owner/name (or pass --repository)")
    ci = data.get("ci")
    if not isinstance(ci, list) or not ci:
        raise ValueError("release manifest requires at least one CI evidence entry")
    for entry in ci:
        if not isinstance(entry, dict) or str(entry.get("status") or "") != "success":
            raise ValueError("each CI evidence entry must have status: success")
        if str(entry.get("commit") or "") != resolved_commit:
            raise ValueError("each CI evidence entry must name the exact release commit")
        url = str(entry.get("url") or "")
        if urlparse(url).hostname != github_host:
            raise ValueError(f"CI evidence must use configured GitHub host {github_host}")
        if f"/{configured_repository}/" not in urlparse(url).path:
            raise ValueError(f"CI evidence must belong to configured repository {configured_repository}")

    artifacts_path = (manifest_path.parent / _required(data, "artifacts")).resolve()
    artifacts = _load_mapping(artifacts_path)
    if artifacts.get("version") != version or artifacts.get("commit") != resolved_commit:
        raise ValueError("artifact manifest version and commit must match the release candidate")
    files = artifacts.get("artifacts")
    if not isinstance(files, list) or not files:
        raise ValueError("artifact manifest requires at least one artifact")
    for artifact in files:
        if not isinstance(artifact, dict):
            raise ValueError("artifact entries must be mappings")
        artifact_path = (artifacts_path.parent / _required(artifact, "path")).resolve()
        expected_digest = _required(artifact, "sha256")
        if not artifact_path.is_file():
            raise ValueError(f"artifact is missing: {artifact_path}")
        if _sha256(artifact_path) != expected_digest:
            raise ValueError(f"artifact digest does not match: {artifact_path}")
    prebuilt = artifacts.get("prebuilt")
    if prebuilt not in {"included", "absent"}:
        raise ValueError("artifact manifest must label prebuilt artifacts as included or absent")

    return [
        f"package version {version} matches {tag}",
        f"tag and candidate commit agree: {resolved_commit}",
        f"CI evidence is successful on {github_host}/{configured_repository}",
        f"release notes and {len(files)} artifact(s) verified; prebuilt artifacts: {prebuilt}",
        f"candidate is ready for manual {release_kind} publication",
    ]
