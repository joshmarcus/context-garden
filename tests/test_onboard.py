from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from garden.checks import run_check
from garden.onboard import _add_github_metadata, discover_project, onboard_project
from garden.planner import run_planner
from garden.store import Store
from tests.conftest import FAKE_CLAUDE, git, write


def _node_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample-web"
    repo.mkdir()
    git("init", "-q", "-b", "trunk", cwd=repo)
    write(repo / "README.md", "# Sample web\n\nA tiny service. Format JavaScript with Prettier.\n")
    write(repo / "CONTRIBUTING.md", "Use conventional commits. Pull requests need one approval.\n")
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
    assert {
        "package.json", "package-lock.json", "src/index.js", ".github/workflows/ci.yml",
    }.issubset(first.read)
    assert ".env" not in first.read
    assert first.trusted_authors == ["maintainer"]
    assert "platform" not in first.trusted_authors
    assert "Formatting: A tiny service. Format JavaScript with Prettier. (source: `README.md`)." in first.conventions
    assert "Review: Use conventional commits. Pull requests need one approval. (source: `CONTRIBUTING.md`)." in first.conventions
    assert "Commits: Use conventional commits. Pull requests need one approval. (source: `CONTRIBUTING.md`)." in first.conventions
    assert "Review: Respect path ownership recorded in CODEOWNERS (source: `.github/CODEOWNERS`)." in first.conventions


@pytest.mark.parametrize("remote", [
    "https://github.com/example/sample-web.git",
    "git@github.com:example/sample-web.git",
    "ssh://git@ssh.github.com:443/example/sample-web.git",
])
def test_onboarding_github_metadata_uses_transport_independent_slug(tmp_path, monkeypatch, remote):
    repo = _node_repo(tmp_path)
    git("remote", "add", "origin", remote, cwd=repo)
    calls = []

    def synthetic_github(_repo, args):
        calls.append(args)
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "trunk"}}
        return []

    monkeypatch.setattr("garden.onboard._gh_json", synthetic_github)
    info = discover_project(repo)
    _add_github_metadata(repo, info)

    assert all("example/sample-web" in " ".join(args) for args in calls)
    assert "github:example/sample-web:repository" in info.read
    assert "github:example/sample-web:open-issues" in info.read
    assert "github:example/sample-web:open-prs" in info.read
    assert "github:example/sample-web:rulesets" in info.read
    assert info.base_branch == "trunk"


def test_onboarding_reports_unavailable_github_metadata_without_fabricating_reads(tmp_path, monkeypatch):
    repo = _node_repo(tmp_path)
    git("remote", "add", "origin", "ssh://git@ssh.github.com:443/example/sample-web.git", cwd=repo)
    monkeypatch.setattr("garden.onboard._gh_json", lambda *_args, **_kwargs: None)

    info = discover_project(repo)
    _add_github_metadata(repo, info)

    assert info.unavailable == [
        "GitHub repository metadata for `example/sample-web`",
        "GitHub open issues for `example/sample-web`",
        "GitHub open pull requests for `example/sample-web`",
        "GitHub repository rulesets for `example/sample-web`",
    ]
    assert not any(item.startswith("github:example/sample-web:") for item in info.read)


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
    assert "trusted author @platform" not in report
    assert "`.github/workflows/ci.yml`" in report
    assert "`src/index.js`" in report
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
        run_planner(store, prompt)
        return _valid_plan(store, prompt)

    onboard_project(repo, garden, planner=fake_harness_planner)

    tasks = Store(garden).product("sample-web").phases[0].tasks
    assert [task.title for task in tasks] == ["Add health endpoint"]
    assert all(task.status.value == "draft" for task in tasks)


def test_onboard_this_repository_preserves_documented_publishing_ci_helper(tmp_path, monkeypatch):
    repo = Path(__file__).parents[1]
    garden = tmp_path / "garden"
    real_run = subprocess.run

    def github_origin(command, *args, **kwargs):
        if command == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/example/context-garden.git\n", "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("garden.onboard.subprocess.run", github_origin)
    info = discover_project(repo)
    backlog_item, source = info.backlog[0]
    real_run = subprocess.run

    def synthetic_public_remote(args, *positional, **kwargs):
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/context-garden.git\n")
        return real_run(args, *positional, **kwargs)

    def self_plan(_store: Store, _prompt: str) -> str:
        item = json.loads(_valid_plan(_store, _prompt))[0]
        item["title"] = backlog_item
        item["body"] = f"## Goal\n\nComplete this discovered item: {backlog_item}\n"
        item["discovered_from"] = f"onboard:{source}"
        return json.dumps([item])

    monkeypatch.setattr("garden.onboard._gh_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("garden.onboard.subprocess.run", synthetic_public_remote)
    onboard_project(repo, garden, planner=self_plan)

    config = yaml.safe_load((garden / "garden.yaml").read_text())
    assert config["products"]["context-garden"]["setup"] == {
        "command": 'uv venv && uv pip install -e ".[dev]"',
        "test": "python3 scripts/check_ci.py",
        "lint": ".venv/bin/ruff check src tests",
        "env": {},
    }
    report = (garden / "context-garden" / "docs" / "onboarding.md").read_text()
    assert "GitHub repository metadata" in report
    assert "GitHub open issues" in report
    assert "GitHub open pull requests" in report
    assert "GitHub repository rulesets" in report
    assert "setup.worker_push: true" in report
    assert "Git/GitHub credentials" in report

    from typer.testing import CliRunner

    from garden.cli import app

    monkeypatch.chdir(garden)
    result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 0, result.output


def test_discovery_reports_ambiguous_ci_commands_without_granting_push_permission(tmp_path):
    repo = tmp_path / "python-app"
    repo.mkdir()
    write(
        repo / "README.md",
        """# Python app

```bash
python -m pip install -e .
pytest -q
ruff check src
python3 scripts/ci.py
```
""",
    )

    info = discover_project(repo)

    assert (info.test_command, info.lint_command) == ("pytest -q", "ruff check src")
    assert info.unavailable == [
        "Whether documented CI command `python3 scripts/ci.py` is a local check or a branch-publishing helper"
    ]
    assert not any("worker_push" in item for item in info.inferred)


def test_publishing_ci_helper_needs_explicit_worker_push_permission():
    result = run_check(
        {"name": "test", "command": "python3 scripts/check_ci.py", "requires_worker_push": True},
        {},
    )

    assert result == {
        "name": "test",
        "status": "fail",
        "summary": "CI helper requires explicit worker push permission",
        "details": "Set products.<name>.setup.worker_push: true and configure the "
                   "worker's Git/GitHub credentials before running this publishing helper.",
    }


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


def test_onboard_rejects_unrelated_task_instead_of_assigning_backlog_by_position(tmp_path):
    repo = _node_repo(tmp_path)

    def unrelated_plan(_store: Store, _prompt: str) -> str:
        item = json.loads(_valid_plan(_store, _prompt))[0]
        item["title"] = "Replace the database engine"
        item["body"] = "## Goal\n\nMigrate all persistence to a different vendor.\n"
        item.pop("discovered_from")
        return json.dumps([item])

    with pytest.raises(ValueError, match="no unambiguous backlog provenance"):
        onboard_project(repo, tmp_path / "garden", planner=unrelated_plan)


def test_onboard_rejects_unrelated_task_even_when_it_claims_a_known_source(tmp_path):
    repo = _node_repo(tmp_path)

    def false_provenance(_store: Store, _prompt: str) -> str:
        item = json.loads(_valid_plan(_store, _prompt))[0]
        item["title"] = "Replace the database engine"
        item["body"] = "## Goal\n\nMigrate all persistence to a different vendor.\n"
        return json.dumps([item])

    with pytest.raises(ValueError, match="no unambiguous backlog provenance"):
        onboard_project(repo, tmp_path / "garden", planner=false_provenance)


def test_onboard_rejects_weak_one_word_provenance_match(tmp_path):
    repo = _node_repo(tmp_path)

    def weak_match(_store: Store, _prompt: str) -> str:
        item = json.loads(_valid_plan(_store, _prompt))[0]
        item["title"] = "Replace the database endpoint"
        item["body"] = "## Goal\n\nMigrate the database endpoint to a different vendor.\n"
        return json.dumps([item])

    with pytest.raises(ValueError, match="no unambiguous backlog provenance"):
        onboard_project(repo, tmp_path / "garden", planner=weak_match)


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


def test_onboard_rolls_back_rejected_plan_and_allows_a_clean_retry(tmp_path):
    from garden.scaffold import init_garden

    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    init_garden(garden, "existing")
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["custom_setting"] = "preserve me"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    write(garden / "notes.md", "An existing garden file.\n")
    before = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}

    def rejected_plan(store: Store, prompt: str) -> str:
        item = json.loads(_valid_plan(store, prompt))[0]
        item["title"] = "Replace the database engine"
        item["body"] = "## Goal\n\nMigrate all persistence to a different vendor.\n"
        item.pop("discovered_from")
        return json.dumps([item])

    with pytest.raises(ValueError, match="Planner output was rejected") as error:
        onboard_project(repo, garden, planner=rejected_plan)

    assert "No tasks were imported or approved" in str(error.value)
    assert f"garden onboard {repo} --into {garden}" in str(error.value)
    after_rejection = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}
    assert after_rejection == before
    assert not (garden / "sample-web").exists()

    created = onboard_project(repo, garden, planner=_valid_plan)

    assert (garden / "sample-web" / "product.md") in created
    tasks = Store(garden).product("sample-web").phases[0].tasks
    assert [task.status.value for task in tasks] == ["draft"]
    assert yaml.safe_load(config_path.read_text())["custom_setting"] == "preserve me"


def test_onboard_rolls_back_a_planner_failure(tmp_path):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"

    def failed_planner(_store: Store, _prompt: str) -> str:
        raise RuntimeError("planner failed (1): unavailable")

    with pytest.raises(ValueError, match=r"planner failed \(1\): unavailable") as error:
        onboard_project(repo, garden, planner=failed_planner)

    assert "No tasks were imported or approved" in str(error.value)
    assert not (garden / "sample-web").exists()


def test_onboard_rolls_back_partial_import_and_allows_a_clean_retry(tmp_path):
    from garden.scaffold import init_garden

    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    init_garden(garden, "existing")
    write(garden / "notes.md", "An existing garden file.\n")
    before = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}

    def partially_invalid_plan(store: Store, prompt: str) -> str:
        first = json.loads(_valid_plan(store, prompt))[0]
        second = {**first, "title": "Add structured logging", "priority": "not-a-number"}
        return json.dumps([first, second])

    with pytest.raises(ValueError, match="invalid literal for int") as error:
        onboard_project(repo, garden, planner=partially_invalid_plan)

    assert "No tasks were imported or approved" in str(error.value)
    after_failure = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}
    assert after_failure == before
    assert not (garden / "sample-web").exists()

    onboard_project(repo, garden, planner=_valid_plan)

    tasks = Store(garden).product("sample-web").phases[0].tasks
    assert [task.status.value for task in tasks] == ["draft"]


def test_onboard_recovery_retry_command_quotes_paths_with_spaces(tmp_path):
    repo = _node_repo(tmp_path).rename(tmp_path / "source project")
    garden = tmp_path / "garden drafts"

    def failed_planner(_store: Store, _prompt: str) -> str:
        raise RuntimeError("planner unavailable")

    with pytest.raises(ValueError, match="Planner output was rejected") as error:
        onboard_project(repo, garden, planner=failed_planner)

    retry = str(error.value).rsplit("Retry with: ", 1)[1]
    assert shlex.split(retry) == ["garden", "onboard", str(repo), "--into", str(garden)]

    onboard_project(repo, garden, planner=_valid_plan)

    assert Store(garden).product("source-project").phases[0].tasks[0].status.value == "draft"


def test_onboard_rejection_keeps_an_owner_edit_to_the_new_draft(tmp_path):
    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"

    def owner_edited_rejected_plan(store: Store, prompt: str) -> str:
        (garden / "sample-web" / "product.md").write_text("Owner's draft edit.\n")
        return "not a JSON plan"

    with pytest.raises(ValueError, match="no JSON array found") as error:
        onboard_project(repo, garden, planner=owner_edited_rejected_plan)

    message = str(error.value)
    recovery_draft = garden / "onboarding-recovery" / "sample-web" / "sample-web" / "product.md"
    assert "garden.yaml: removed" in message
    assert ".gitignore: removed" in message
    assert "sample-web/product.md: owner edit retained at onboarding-recovery/sample-web/sample-web/product.md" in message
    assert f"Retry with: garden onboard {repo} --into {garden}" in message
    assert recovery_draft.read_text() == "Owner's draft edit.\n"
    assert not (garden / "sample-web").exists()

    onboard_project(repo, garden, planner=_valid_plan)

    assert recovery_draft.read_text() == "Owner's draft edit.\n"
    assert Store(garden).product("sample-web").phases[0].tasks[0].status.value == "draft"


def test_onboard_recovery_restores_a_generated_file_deleted_by_the_planner(tmp_path):
    from garden.scaffold import init_garden

    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    init_garden(garden, "existing")
    config_path = garden / "garden.yaml"
    before = config_path.read_bytes()

    def deleting_rejected_plan(_store: Store, _prompt: str) -> str:
        config_path.unlink()
        return "not a JSON plan"

    with pytest.raises(ValueError, match="no JSON array found") as error:
        onboard_project(repo, garden, planner=deleting_rejected_plan)

    assert "garden.yaml: restored after planner removed it" in str(error.value)
    assert config_path.read_bytes() == before


def test_init_scaffolds_onboard_skill(tmp_path):
    from garden.scaffold import init_garden

    init_garden(tmp_path, "demo")
    skill = tmp_path / ".claude" / "skills" / "garden-onboard" / "SKILL.md"
    assert skill.exists()
    assert "garden onboard <path-or-url>" in skill.read_text()


def test_onboard_preserves_existing_trusted_authors(tmp_path):
    from garden.scaffold import init_garden

    repo = _node_repo(tmp_path)
    garden = tmp_path / "garden"
    init_garden(garden, "existing")
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["github"]["trusted_authors"] = ["existing-owner"]
    config_path.write_text(yaml.safe_dump(config))

    onboard_project(repo, garden, planner=_valid_plan)

    config = yaml.safe_load(config_path.read_text())
    assert config["github"]["trusted_authors"] == ["existing-owner", "maintainer"]
