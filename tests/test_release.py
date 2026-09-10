from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from garden.cli import app
from garden.release import validate_candidate


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _candidate(tmp_path: Path, *, host: str = "forge.example.test") -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'context-garden'\nversion = '1.2.3'\n")
    (tmp_path / "notes.md").write_text("# Release notes\n")
    artifact = tmp_path / "context-garden-1.2.3.tar.gz"
    artifact.write_bytes(b"source distribution")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "candidate")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "tag", "-am", "release 1.2.3", "v1.2.3")
    (tmp_path / "artifacts.yaml").write_text(yaml.safe_dump({
        "version": "1.2.3", "commit": commit, "prebuilt": "absent",
        "artifacts": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    }))
    manifest = tmp_path / "candidate.yaml"
    manifest.write_text(yaml.safe_dump({
        "version": "1.2.3", "tag": "v1.2.3", "commit": commit, "kind": "prerelease",
        "repository": "team/garden", "notes": "notes.md", "artifacts": "artifacts.yaml",
        "ci": [{"status": "success", "commit": commit,
                "url": f"https://{host}/team/garden/actions/runs/12"}],
    }))
    return manifest


def test_validate_candidate_checks_exact_tag_ci_notes_and_artifacts(tmp_path):
    manifest = _candidate(tmp_path)

    result = validate_candidate(manifest, root=tmp_path, github_host="forge.example.test")

    assert "tag and candidate commit agree" in result[1]
    assert result[-1] == "candidate is ready for manual prerelease publication"


def test_validate_candidate_rejects_mismatched_artifact_digest(tmp_path):
    manifest = _candidate(tmp_path)
    data = yaml.safe_load((tmp_path / "artifacts.yaml").read_text())
    data["artifacts"][0]["sha256"] = "wrong"
    (tmp_path / "artifacts.yaml").write_text(yaml.safe_dump(data))

    try:
        validate_candidate(manifest, root=tmp_path, github_host="forge.example.test")
    except ValueError as exc:
        assert "artifact digest" in str(exc)
    else:  # pragma: no cover - protects the assertion's intended failure path
        raise AssertionError("candidate with a wrong digest passed")


def test_validate_candidate_requires_a_full_canonical_commit(tmp_path):
    manifest = _candidate(tmp_path)
    data = yaml.safe_load(manifest.read_text())
    full_commit = data["commit"]

    for commit in ("HEAD", full_commit[:12]):
        data["commit"] = commit
        manifest.write_text(yaml.safe_dump(data))
        try:
            validate_candidate(manifest, root=tmp_path, github_host="forge.example.test")
        except ValueError as exc:
            assert "full canonical commit" in str(exc)
        else:  # pragma: no cover - protects the assertion's intended failure path
            raise AssertionError(f"candidate with {commit!r} commit passed")


def test_release_validate_command_accepts_github_enterprise_host(tmp_path, monkeypatch):
    manifest = _candidate(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["release", "validate", "--manifest", str(manifest),
                                      "--github-host", "forge.example.test"])

    assert result.exit_code == 0, result.output
    assert "CI evidence is successful on forge.example.test/team/garden" in result.output
