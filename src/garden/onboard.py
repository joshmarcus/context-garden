"""Deterministic project discovery and garden onboarding.

Discovery deliberately reads only project metadata and text documentation. Secret files
such as ``.env`` are noted by name but are never opened; the planner only receives the
draft context written from the redacted discovery result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import CONFIG_NAME
from .planner import import_plan, parse_plan, plan_prompt, run_planner
from .scaffold import init_garden, new_phase
from .store import Store


@dataclass
class ProjectDiscovery:
    name: str
    base_branch: str
    setup_command: str
    test_command: str
    lint_command: str
    module_map: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    backlog: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    configure_by_hand: list[str] = field(default_factory=list)
    trusted_authors: list[str] = field(default_factory=list)


_SECRET_FILES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc"}
_SKIP_DIRS = {".git", ".garden", "node_modules", ".venv", "venv", "dist", "build", "target"}
_DOC_NAMES = {"readme", "contributing", "architecture", "agents", "todo", "roadmap", "codeowners"}


def _text(path: Path, limit: int = 80_000) -> str:
    try:
        return path.read_text(errors="replace")[:limit]
    except OSError:
        return ""


def _project_files(repo: Path) -> list[Path]:
    return sorted(
        p for p in repo.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(repo).parts)
    )


def _gh_json(repo: Path, args: list[str]) -> object | None:
    """Return optional GitHub metadata. Failure means simply that this source was unavailable."""
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=15
        )
        return json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _add_github_metadata(repo: Path, result: ProjectDiscovery) -> None:
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True, check=False
    ).stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        return
    slug = f"{match.group(1)}/{match.group(2)}"
    repo_info = _gh_json(repo, ["repo", "view", slug, "--json", "defaultBranchRef"])
    if isinstance(repo_info, dict):
        default = (repo_info.get("defaultBranchRef") or {}).get("name")
        if default:
            result.base_branch = str(default)
            result.inferred.append(f"base branch from GitHub repository metadata: {default}")
        result.read.append(f"github:{slug}:repository")
    issues = _gh_json(repo, ["issue", "list", "--repo", slug, "--state", "open", "--limit", "100", "--json", "number,title,milestone"])
    if isinstance(issues, list):
        result.read.append(f"github:{slug}:open-issues")
        for issue in issues:
            if isinstance(issue, dict) and issue.get("title"):
                milestone = (issue.get("milestone") or {}).get("title") if isinstance(issue.get("milestone"), dict) else ""
                suffix = f" [milestone: {milestone}]" if milestone else ""
                result.backlog.append(f"GitHub issue #{issue.get('number')}: {issue['title']}{suffix}")
    prs = _gh_json(repo, ["pr", "list", "--repo", slug, "--state", "open", "--limit", "100", "--json", "number,title"])
    if isinstance(prs, list):
        result.read.append(f"github:{slug}:open-prs")
        result.inferred.append(f"{len(prs)} open pull request(s) found; review them before approving overlapping drafts")
    rules = _gh_json(repo, ["api", f"repos/{slug}/rulesets"])
    if isinstance(rules, list):
        result.read.append(f"github:{slug}:rulesets")
        result.inferred.append(f"{len(rules)} repository ruleset(s) found; preserve their review and branch requirements")


def _commands(repo: Path, files: list[Path]) -> tuple[str, str, str, list[str]]:
    names = {p.relative_to(repo).as_posix() for p in files}
    ci = "\n".join(_text(p) for p in files if p.relative_to(repo).as_posix().startswith(".github/workflows/"))
    if "package.json" in names:
        package = yaml.safe_load((repo / "package.json").read_text()) or {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        setup = "npm ci" if "package-lock.json" in names else "npm install"
        test = "npm test" if "test" in scripts else ""
        lint = "npm run lint" if "lint" in scripts else ""
        return setup, test, lint, ["package.json"]
    if "go.mod" in names:
        return "go mod download", "go test ./...", "go vet ./...", ["go.mod"]
    if "Cargo.toml" in names:
        return "cargo fetch --locked", "cargo test --locked", "cargo clippy --all-targets -- -D warnings", ["Cargo.toml"]
    if "pyproject.toml" in names:
        ci_commands = [m.strip() for m in re.findall(r"^\s*-\s*run:\s*(.+)$", ci, re.MULTILINE)]
        setup = next((c for c in ci_commands if re.search(r"(?:pip install|uv sync|uv pip install)", c)), "")
        setup = setup or ("uv sync --all-extras" if "uv.lock" in names else "python -m pip install -e '.[dev]'")
        test = next((c for c in ci_commands if re.search(r"(?:^|\s)(?:pytest|python -m pytest)(?:\s|$)", c)), "")
        test = test or ("pytest -q" if "[tool.pytest" in _text(repo / "pyproject.toml") else "")
        lint = next((c for c in ci_commands if re.search(r"(?:^|\s)ruff check(?:\s|$)", c)), "")
        lint = lint or ("ruff check src tests" if "[tool.ruff" in _text(repo / "pyproject.toml") else "")
        return setup, test, lint, ["pyproject.toml"] + (["uv.lock"] if "uv.lock" in names else [])
    if "pom.xml" in names:
        return "./mvnw dependency:go-offline", "./mvnw test", "", ["pom.xml"]
    if "Gemfile" in names:
        return "bundle install", "bundle exec rake test", "", ["Gemfile"]
    return "", "", "", []


def discover_project(repo: Path) -> ProjectDiscovery:
    """Inspect a local checkout without network or model calls."""
    repo = repo.resolve()
    files = _project_files(repo)
    rels = {p.relative_to(repo).as_posix(): p for p in files}
    try:
        current = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=False
        ).stdout.strip()
        remote_head = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo, capture_output=True, text=True, check=False,
        ).stdout.strip()
        base = remote_head.removeprefix("origin/") or current or "main"
    except OSError:
        base = "main"
    setup, test, lint, command_sources = _commands(repo, files)
    result = ProjectDiscovery(repo.name, base, setup, test, lint)
    result.read.extend(command_sources)
    result.inferred.extend(
        f"{label} command from {', '.join(command_sources)} and CI metadata: {value or 'not determined'}"
        for label, value in (("setup", setup), ("test", test), ("lint", lint))
    )

    for rel, path in rels.items():
        lower = path.name.lower().split(".")[0]
        is_doc = lower in _DOC_NAMES or rel.startswith("docs/") or rel.startswith(".github/ISSUE_TEMPLATE/")
        if is_doc and path.name not in _SECRET_FILES:
            result.read.append(rel)
            text = _text(path)
            if lower in {"todo", "roadmap"}:
                result.backlog.extend(line.lstrip("- *").strip() for line in text.splitlines() if line.lstrip().startswith(("-", "*")))
        if path.name in _SECRET_FILES or path.name.startswith(".env."):
            result.configure_by_hand.append(f"Secret file {rel} exists; configure its values by hand (file not read).")

    source_exts = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb"}
    for path in files:
        if path.suffix in source_exts:
            for match in re.finditer(r"\b(?:TODO|FIXME)(?:\(([^)]+)\))?[: ]+([^\n]+)", _text(path, 30_000)):
                owner = f" ({match.group(1)})" if match.group(1) else ""
                result.backlog.append(f"{match.group(2).strip()}{owner} — {path.relative_to(repo)}")

    ci_text = "\n".join(_text(p) for rel, p in rels.items() if rel.startswith(".github/workflows/"))
    for name in sorted(set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", ci_text))):
        result.configure_by_hand.append(f"Environment variable {name} is referenced by CI; configure by hand.")
    codeowners = next((p for rel, p in rels.items() if p.name == "CODEOWNERS"), None)
    if codeowners:
        result.trusted_authors = sorted(set(re.findall(r"(?<!\w)@([\w.-]+)", _text(codeowners))))
    result.module_map = sorted(
        p.name + ("/" if p.is_dir() else "") for p in repo.iterdir()
        if p.name not in _SKIP_DIRS and not p.name.startswith(".")
    )[:40]
    result.conventions = [
        f"Use the project's discovered test command: `{test}`." if test else "Choose and document a test command.",
        f"Use the project's discovered lint command: `{lint}`." if lint else "Choose and document a lint command.",
        f"Target the `{base}` branch for changes.",
    ]
    result.inferred.append(f"base branch from the current Git checkout: {base}")
    result.inferred.append("module map from top-level repository entries")
    _add_github_metadata(repo, result)
    result.read = sorted(set(result.read))
    return result


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "product"


def _prefix(name: str) -> str:
    letters = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", name) if part)
    return (letters or re.sub(r"[^A-Za-z]", "", name))[:4].upper() or "PRJ"


def onboard_project(
    repo: Path,
    garden: Path,
    *,
    repo_value: str | None = None,
    planner: Callable[[Store, str], str] = run_planner,
) -> list[Path]:
    """Create or extend a garden from a checkout and draft its first planned tasks."""
    info = discover_project(repo)
    garden = garden.resolve()
    created = init_garden(garden, f"{info.name} garden")
    product = _slug(info.name)
    cfg_path = garden / CONFIG_NAME
    config = yaml.safe_load(cfg_path.read_text()) or {}
    configured_repo = repo_value or os.path.relpath(repo.resolve(), garden)
    env_names = {
        m.group(1): ""
        for note in info.configure_by_hand
        if (m := re.match(r"Environment variable ([A-Za-z_][A-Za-z0-9_]*)", note))
    }
    config.setdefault("products", {})[product] = {
        "repo": configured_repo,
        "base_branch": info.base_branch,
        "id_prefix": _prefix(info.name),
        "setup": {"command": info.setup_command, "test": info.test_command, "lint": info.lint_command, "env": env_names},
        "onboarding_notes": [
            f"{label} command could not be determined; configure by hand."
            for label, value in (("setup", info.setup_command), ("test", info.test_command), ("lint", info.lint_command))
            if not value
        ],
    }
    if info.trusted_authors:
        config.setdefault("github", {})["trusted_authors"] = info.trusted_authors
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False))
    created.append(cfg_path)

    product_dir = garden / product
    product_dir.mkdir(exist_ok=True)
    overview = product_dir / "product.md"
    overview.write_text(
        f"# {info.name}\n\nAn existing project onboarded for maintainers and contributors. Edit this draft to describe its purpose and users.\n\n"
        f"## Repo\n\nCode lives in `{configured_repo}` (base branch `{info.base_branch}`).\n\n"
        "## Module map\n\n" + "\n".join(f"- `{entry}`" for entry in info.module_map) + "\n\n"
        f"## Commands\n\n- Setup: `{info.setup_command or 'not determined'}`\n- Test: `{info.test_command or 'not determined'}`\n- Lint: `{info.lint_command or 'not determined'}`\n\n"
        "## Conventions\n\n" + "\n".join(f"- {item}" for item in info.conventions) + "\n"
    )
    created.append(overview)
    conventions = garden / "principles" / f"10-{product}-conventions.md"
    conventions.write_text(f"# {info.name} conventions (draft)\n\n" + "\n".join(f"- {item}" for item in info.conventions) + "\n")
    created.append(conventions)

    store = Store(garden)
    created.extend(new_phase(store, product, "phase-01"))
    goals = garden / product / "phase-01" / "goals.md"
    backlog = info.backlog[:50] or ["No existing backlog item was found; choose the first deliverable."]
    goals.write_text(
        "# phase-01 goals (draft)\n\n## Why this phase\n\nTurn the existing project backlog into the first garden-managed slice.\n\n"
        "## Goals\n\n" + "\n".join(f"{i}. {item}" for i, item in enumerate(backlog, 1)) + "\n\n"
        "## Non-goals\n\n- Work not represented in the discovered backlog.\n\n## Definition of done\n\n- The approved first-phase tasks are complete.\n"
    )
    store.invalidate()
    raw = planner(store, plan_prompt(store, product, "phase-01", extra="All tasks are onboarding drafts. Use only backlog items stated in the goals."))
    tasks = import_plan(store, product, "phase-01", parse_plan(raw), status="draft")
    for task in tasks:
        task.discovered_from = "onboard:project-backlog"
        store.save(task)
        created.append(task.path)

    report = product_dir / "docs" / "onboarding.md"
    report.parent.mkdir(exist_ok=True)
    open_decisions = [
        "Confirm or edit the product purpose, users and module map.", "Approve, edit or cancel every draft task.",
        "Choose model tiers and phase budgets.", "Configure notifications and the automerge policy.",
        *info.configure_by_hand,
    ]
    report.write_text(
        "# Onboarding report (draft)\n\n## Read\n\n" + "\n".join(f"- `{p}`" for p in info.read) + "\n\n"
        "## Inferences\n\n" + "\n".join(f"- {item}" for item in info.inferred) + "\n\n"
        "## Could not determine\n\n" + "\n".join(f"- {label} command" for label, value in (("setup", info.setup_command), ("test", info.test_command), ("lint", info.lint_command)) if not value) + "\n\n"
        "## Decisions to make\n\n" + "\n".join(f"- {item}" for item in open_decisions) + "\n"
    )
    created.append(report)
    return created


def onboard_source(source: str, garden: Path, planner: Callable[[Store, str], str] = run_planner) -> list[Path]:
    """Onboard a local path or clone a git URL for discovery."""
    local = Path(source).expanduser()
    if local.exists():
        return onboard_project(local, garden, planner=planner)
    with tempfile.TemporaryDirectory(prefix="garden-onboard-") as tmp:
        proc = subprocess.run(["git", "clone", "--quiet", source, tmp], capture_output=True, text=True, check=False)
        if proc.returncode:
            raise ValueError(f"could not clone {source}: {proc.stderr.strip()}")
        return onboard_project(Path(tmp), garden, repo_value=source, planner=planner)
