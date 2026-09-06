from __future__ import annotations

import json
from pathlib import Path

import yaml

from garden.onboard import discover_project, onboard_project
from garden.planner import run_planner
from garden.store import Store
from tests.conftest import FAKE_CLAUDE, git, write


def _node_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample-web"
    repo.mkdir()
    git("init", "-q", "-b", "trunk", cwd=repo)
    write(repo / "README.md", "# Sample web\n\nA tiny service.\n")
    write(repo / "package.json", json.dumps({"scripts": {"test": "node --test", "lint": "eslint ."}}))
    write(repo / "package-lock.json", "{}")
    write(repo / "src" / "index.js", "// TODO(alex): add a health endpoint\n")
    write(repo / "TODO.md", "# Roadmap\n\n- Add structured logging\n")
    write(repo / ".github" / "CODEOWNERS", "* @maintainer @platform/team\n")
    write(repo / ".github" / "workflows" / "ci.yml", "env:\n  TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n")
    write(repo / ".env", "DEPLOY_TOKEN=super-secret-value\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def _valid_plan(_store: Store, _prompt: str) -> str:
    return json.dumps([
        {
            "title": "Add health endpoint",
            "priority": 1,
            "estimate": "S",
            "difficulty": "easy",
            "depends_on": [],
            "reading": [],
            "body": "## Goal\n\nAdd health output.\n\n## Context\n\nDiscovered backlog item.\n\n## Acceptance criteria\n\n- [ ] A test covers the endpoint.\n\n## Out of scope\n\n- Deployment changes.\n",
        }
    ])


def test_discovery_is_deterministic_and_does_not_read_secret_values(tmp_path, monkeypatch):
    repo = _node_repo(tmp_path)
    git("remote", "add", "origin", "git@github.com:example/sample-web.git", cwd=repo)

    def unexpected_github_query(*_args, **_kwargs):
        raise AssertionError("deterministic discovery must not query GitHub")

    monkeypatch.setattr("garden.onboard._gh_json", unexpected_github_query)
    first = discover_project(repo)
    second = discover_project(repo)

    assert first == second
    assert (first.setup_command, first.test_command, first.lint_command) == ("npm ci", "npm test", "npm run lint")
    assert first.base_branch == "trunk"
    rendered = repr(first)
    assert "super-secret-value" not in rendered
    assert any("DEPLOY_TOKEN" in item and "configure by hand" in item for item in first.configure_by_hand)
    assert any("file not read" in item for item in first.configure_by_hand)


def test_onboard_node_project_writes_complete_drafts_and_report(tmp_path, monkeypatch):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"

    onboard_project(repo, garden, planner=_valid_plan)
    store = Store(garden)
    product = store.product("sample-web")
    task = product.phases[0].tasks[0]
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    setup = config["products"]["sample-web"]["setup"]

    assert setup == {"command": "npm ci", "test": "npm test", "lint": "npm run lint", "env": {"DEPLOY_TOKEN": ""}}
    assert task.status.value == "draft"
    assert task.discovered_from == "onboard:project-backlog"
    assert (garden / "sample-web" / "product.md").exists()
    assert (garden / "principles" / "10-sample-web-conventions.md").exists()
    report = (garden / "sample-web" / "docs" / "onboarding.md").read_text()
    assert all(section in report for section in ("## Read", "## Inferences and provenance", "## Could not determine", "## Decisions to make"))
    assert "trusted author @maintainer from CODEOWNERS" in report
    assert "backlog item from `TODO.md`: Add structured logging" in report
    assert "backlog item from `src/index.js`: add a health endpoint" in report
    assert "super-secret-value" not in "\n".join(p.read_text(errors="replace") for p in garden.rglob("*") if p.is_file())

    from typer.testing import CliRunner

    from garden.cli import app

    monkeypatch.chdir(garden)
    result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 0, result.output


def test_onboard_planner_step_uses_fake_harness(tmp_path, monkeypatch):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "plan")

    def fake_harness_planner(store: Store, prompt: str) -> str:
        config = yaml.safe_load((garden / "garden.yaml").read_text())
        config.setdefault("harnesses", {}).setdefault("claude", {})["bin"] = str(FAKE_CLAUDE)
        config.setdefault("worker_env", {})["pass"] = ["FAKE_CLAUDE_*", "PYTHONPATH"]
        (garden / "garden.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        store.invalidate()
        return run_planner(store, prompt)

    onboard_project(repo, garden, planner=fake_harness_planner)

    tasks = Store(garden).product("sample-web").phases[0].tasks
    assert [task.title for task in tasks] == ["First planned task", "Second planned task"]
    assert all(task.status.value == "draft" for task in tasks)


def test_onboard_refuses_existing_product_without_changing_it(tmp_path):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    onboard_project(repo, garden, planner=_valid_plan)
    before = {p.relative_to(garden): p.read_bytes() for p in garden.rglob("*") if p.is_file()}

    import pytest

    with pytest.raises(ValueError, match="would overwrite an existing product"):
        onboard_project(repo, garden, planner=_valid_plan)

    after = {p.relative_to(garden): p.read_bytes() for p in garden.rglob("*") if p.is_file()}
    assert after == before


def test_init_scaffolds_onboard_skill(tmp_path):
    from garden.scaffold import init_garden

    init_garden(tmp_path, "demo")
    skill = tmp_path / ".claude" / "skills" / "garden-onboard" / "SKILL.md"
    assert skill.exists()
    assert "garden onboard <path-or-url>" in skill.read_text()
