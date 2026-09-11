from __future__ import annotations

import ast
import importlib.util
import os
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def load_distribution_verifier():
    path = ROOT / "scripts/verify_distribution.py"
    spec = importlib.util.spec_from_file_location("verify_distribution", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distribution_metadata_and_import_version_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    module = ast.parse((ROOT / "src/garden/__init__.py").read_text())
    version_assignment = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets)
    )

    assert ast.literal_eval(version_assignment.value) == project["project"]["version"]
    assert project["project"]["name"] == "context-garden"
    assert project["project"]["scripts"]["garden"] == "garden.cli:app"
    assert project["project"]["requires-python"] == ">=3.11"
    assert project["project"]["license"] == "MIT"


def test_distribution_build_includes_runtime_resources_and_excludes_repository_state():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build = project["tool"]["hatch"]["build"]["targets"]

    assert build["wheel"]["packages"] == ["src/garden"]
    assert build["sdist"]["include"] == [
        "/LICENSE", "/README.md", "/pyproject.toml", "/src/garden"
    ]
    assert (ROOT / "src/garden/web/templates/base.html").is_file()
    assert (ROOT / "src/garden/web/static/plates/README.md").is_file()


def test_pypi_publication_is_an_explicit_trusted_release():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-pypi.yml").read_text())
    # YAML 1.1 treats the key `on` as boolean True; GitHub Actions uses YAML 1.2.
    trigger = workflow.get("on", workflow.get(True))
    publish = workflow["jobs"]["publish"]

    assert trigger == {"release": {"types": ["published"]}}
    assert workflow["jobs"]["build"]["if"] == "${{ !github.event.release.prerelease }}"
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["steps"][-1]["uses"] == "pypa/gh-action-pypi-publish@release/v1"


def test_distribution_verifier_removes_python_source_path_overrides(monkeypatch):
    verifier = load_distribution_verifier()
    monkeypatch.setenv("PYTHONHOME", "/source/python")
    monkeypatch.setenv("PYTHONPATH", "/source/checkout")

    environment = verifier.clean_environment()

    assert "PYTHONHOME" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["PATH"] == os.environ["PATH"]


def test_distribution_verifier_runs_checks_from_disposable_directory(tmp_path, monkeypatch):
    verifier = load_distribution_verifier()
    artifact = tmp_path / "context_garden-0.3.1-py3-none-any.whl"
    artifact.touch()
    calls = []
    monkeypatch.setattr(verifier, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(verifier.sys, "argv", [
        "verify_distribution.py", str(artifact), "--version", "0.3.1",
    ])
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))

    verifier.main()

    working_directories = {kwargs["cwd"] for _, kwargs in calls}
    assert len(working_directories) == 1
    assert all(not cwd.resolve().is_relative_to(ROOT.resolve()) for cwd in working_directories)
    assert all("PYTHONPATH" not in kwargs["env"] for _, kwargs in calls)
    installed_check = next(args[2] for args, _ in calls if args[1:2] == ("-c",))
    assert "Path(garden.__file__).resolve().is_relative_to(environment)" in installed_check
    assert "Path(installed.locate_file('')).resolve().is_relative_to(environment)" in installed_check
