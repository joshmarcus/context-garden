from __future__ import annotations

import json
from pathlib import Path

import pytest
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
            "discovered_from": "onboard:src/index.js",
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


def test_discovery_uses_manifest_name_in_a_worktree_directory(tmp_path):
    repo = tmp_path / "CG-215"
    repo.mkdir()
    write(repo / "pyproject.toml", '[project]\nname = "context-garden"\n')

    assert discover_project(repo).name == "context-garden"


def test_discovery_records_remote_head_as_base_branch_source(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git("init", "-q", "-b", "work", cwd=repo)
    write(repo / "README.md", "# Project\n")
    git("add", "README.md", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    git("update-ref", "refs/remotes/origin/trunk", "HEAD", cwd=repo)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk", cwd=repo)

    info = discover_project(repo)

    assert info.base_branch == "trunk"
    assert info.base_branch_source == "git remote HEAD"
    assert "base branch `trunk` from git remote HEAD" in info.inferred


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
    assert task.discovered_from == "onboard:src/index.js"
    assert (garden / "sample-web" / "product.md").exists()
    assert (garden / "principles" / "10-sample-web-conventions.md").exists()
    report = (garden / "sample-web" / "docs" / "onboarding.md").read_text()
    assert all(section in report for section in ("## Read", "## Inferences and provenance", "## Could not determine", "## Decisions to make"))
    assert "trusted author @maintainer from CODEOWNERS" in report
    assert "backlog item from `TODO.md`: Add structured logging" in report
    assert "backlog item from `src/index.js`: add a health endpoint" in report
    assert "project name `sample-web` from repository directory" in report
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


def test_onboard_this_repository_uses_documented_development_commands(tmp_path, monkeypatch):
    repo = Path(__file__).parents[1]
    garden = tmp_path / "garden"
    info = discover_project(repo)
    source = info.backlog[0][1]

    def self_plan(_store: Store, _prompt: str) -> str:
        item = json.loads(_valid_plan(_store, _prompt))[0]
        item["discovered_from"] = f"onboard:{source}"
        return json.dumps([item])

    monkeypatch.setattr("garden.onboard._gh_json", lambda *_args, **_kwargs: None)
    onboard_project(repo, garden, planner=self_plan)

    config = yaml.safe_load((garden / "garden.yaml").read_text())
    assert config["products"]["context-garden"]["setup"] == {
        "command": 'uv venv && uv pip install -e ".[dev]"',
        "test": "PYTHONPATH=src .venv/bin/python -m pytest -q",
        "lint": ".venv/bin/ruff check src tests",
        "env": {},
    }
    report = (garden / "context-garden" / "docs" / "onboarding.md").read_text()
    assert "GitHub repository metadata" in report
    assert "GitHub open issues" in report
    assert "GitHub open pull requests" in report
    assert "GitHub repository rulesets" in report

    from typer.testing import CliRunner

    from garden.cli import app

    monkeypatch.chdir(garden)
    result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("planner_provenance", [None, "onboard:not-a-real-source"])
def test_onboard_repairs_bad_planner_provenance_to_exact_backlog_source(tmp_path, planner_provenance):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"

    def plan_with_bad_provenance(store: Store, prompt: str) -> str:
        item = json.loads(_valid_plan(store, prompt))[0]
        if planner_provenance is None:
            item.pop("discovered_from")
        else:
            item["discovered_from"] = planner_provenance
        return json.dumps([item])

    onboard_project(repo, garden, planner=plan_with_bad_provenance)

    task = Store(garden).product("sample-web").phases[0].tasks[0]
    assert task.discovered_from == "onboard:src/index.js"


def test_discovery_uses_safe_environment_sources_and_reports_exact_provenance(tmp_path):
    repo = tmp_path / "checkout-name"
    repo.mkdir()
    git("init", "-q", "-b", "work", cwd=repo)
    write(repo / "package.json", json.dumps({"name": "actual-product"}))
    write(repo / "Makefile", "setup:\n\ttool install\ntest:\n\ttool test\nlint:\n\ttool lint\n")
    write(repo / "Dockerfile", "FROM scratch\n")
    write(repo / ".devcontainer" / "devcontainer.json", "{}")
    write(repo / ".pre-commit-config.yaml", "repos: []\n")
    write(repo / ".github" / "PULL_REQUEST_TEMPLATE" / "change.md", "Run checks before review.\n")
    write(
        repo / ".github" / "workflows" / "ci.yml",
        "steps:\n  - run: API_TOKEN=plain-text-secret pytest -q\n",
    )

    info = discover_project(repo)

    assert info.name == "actual-product"
    assert info.name_source == "package.json name"
    assert info.base_branch == "work"
    assert info.base_branch_source == "current Git branch"
    assert (info.setup_command, info.test_command, info.lint_command) == ("make setup", "make test", "make lint")
    assert "plain-text-secret" not in repr(info)
    assert {
        "Makefile", "Dockerfile", ".devcontainer/devcontainer.json", ".pre-commit-config.yaml",
        ".github/PULL_REQUEST_TEMPLATE/change.md",
    }.issubset(set(info.read))


def test_secret_bearing_ci_command_is_never_written(tmp_path):
    repo = tmp_path / "python-app"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    write(repo / "pyproject.toml", '[project]\nname = "python-app"\n[tool.pytest.ini_options]\n')
    write(repo / ".github" / "workflows" / "ci.yml", "steps:\n  - run: API_TOKEN=hunter2 pytest -q\n")
    garden = tmp_path / "garden"

    onboard_project(repo, garden, planner=_valid_plan)

    output = "\n".join(path.read_text(errors="replace") for path in garden.rglob("*") if path.is_file())
    assert "hunter2" not in output
    assert yaml.safe_load((garden / "garden.yaml").read_text())["products"]["python-app"]["setup"]["test"] == "pytest -q"


@pytest.mark.parametrize(
    ("filename", "contents", "expected"),
    [
        ("justfile", "setup:\n  tool install\ntest:\n  tool test\nlint:\n  tool lint\n", ("just setup", "just test", "just lint")),
        (
            "Taskfile.yml",
            "version: '3'\ntasks:\n  setup: {cmds: ['tool install']}\n  test: {cmds: ['tool test']}\n  lint: {cmds: ['tool lint']}\n",
            ("task setup", "task test", "task lint"),
        ),
    ],
)
def test_discovery_uses_supported_task_runners(tmp_path, filename, contents, expected):
    repo = tmp_path / "project"
    repo.mkdir()
    write(repo / filename, contents)

    info = discover_project(repo)

    assert (info.setup_command, info.test_command, info.lint_command) == expected
    assert filename in info.read


def test_onboard_refuses_existing_product_without_changing_it(tmp_path):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    onboard_project(repo, garden, planner=_valid_plan)
    before = {p.relative_to(garden): p.read_bytes() for p in garden.rglob("*") if p.is_file()}

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
