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
import tomllib
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
    backlog: list[tuple[str, str]] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    configure_by_hand: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    trusted_authors: list[str] = field(default_factory=list)
    name_source: str = "repository directory"
    base_branch_source: str = "default fallback"


_SECRET_FILES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc"}
_SKIP_DIRS = {".git", ".garden", "node_modules", ".venv", "venv", "dist", "build", "target"}
_DOC_NAMES = {"readme", "contributing", "architecture", "agents", "todo", "roadmap", "codeowners"}
_SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb"}
_ENVIRONMENT_FILES = {
    "pyproject.toml", "uv.lock", "package.json", "package-lock.json", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "pom.xml", "Gemfile", "Gemfile.lock", "Makefile", "justfile",
    "Taskfile.yml", "Taskfile.yaml", "Dockerfile", ".pre-commit-config.yaml",
}


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
            result.base_branch_source = "GitHub repository metadata"
            result.inferred = [item for item in result.inferred if not item.startswith("base branch `")]
            result.inferred.append(f"base branch from GitHub repository metadata: {default}")
        result.read.append(f"github:{slug}:repository")
    else:
        result.unavailable.append(f"GitHub repository metadata for `{slug}`")
    issues = _gh_json(repo, ["issue", "list", "--repo", slug, "--state", "open", "--limit", "100", "--json", "number,title,milestone"])
    if isinstance(issues, list):
        result.read.append(f"github:{slug}:open-issues")
        for issue in issues:
            if isinstance(issue, dict) and issue.get("title"):
                milestone = (issue.get("milestone") or {}).get("title") if isinstance(issue.get("milestone"), dict) else ""
                suffix = f" [milestone: {milestone}]" if milestone else ""
                result.backlog.append((f"GitHub issue #{issue.get('number')}: {issue['title']}{suffix}", f"github:{slug}:issue-{issue.get('number')}"))
    else:
        result.unavailable.append(f"GitHub open issues for `{slug}`")
    prs = _gh_json(repo, ["pr", "list", "--repo", slug, "--state", "open", "--limit", "100", "--json", "number,title"])
    if isinstance(prs, list):
        result.read.append(f"github:{slug}:open-prs")
        result.inferred.append(f"{len(prs)} open pull request(s) found; review them before approving overlapping drafts")
    else:
        result.unavailable.append(f"GitHub open pull requests for `{slug}`")
    rules = _gh_json(repo, ["api", f"repos/{slug}/rulesets"])
    if isinstance(rules, list):
        result.read.append(f"github:{slug}:rulesets")
        result.inferred.append(f"{len(rules)} repository ruleset(s) found; preserve their review and branch requirements")
    else:
        result.unavailable.append(f"GitHub repository rulesets for `{slug}`")


def _safe_ci_command(command: str) -> bool:
    """CI is documentation, not trusted command input: never copy credential-bearing text."""
    secret_assignment = r"\b[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL|AUTH)[A-Za-z_]*\s*="
    return not (
        re.search(r"\$\{\{|secrets\.|\$\(|`|https?://[^\s/:]+:[^\s/@]+@", command, re.IGNORECASE)
        or re.search(secret_assignment, command, re.IGNORECASE)
    )


def _runner_commands(repo: Path, names: set[str]) -> tuple[str, str, str, list[str]]:
    """Infer commands from task-runner target names, never from their shell recipes."""
    candidates: list[tuple[str, str, set[str]]] = []
    if "Makefile" in names:
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?![=])", _text(repo / "Makefile"), re.MULTILINE))
        candidates.append(("Makefile", "make", targets))
    if "justfile" in names:
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+)(?:\s+[^:=\n]+)?\s*:(?:\s|$)", _text(repo / "justfile"), re.MULTILINE))
        candidates.append(("justfile", "just", targets))
    taskfile = next((name for name in ("Taskfile.yml", "Taskfile.yaml") if name in names), "")
    if taskfile:
        try:
            data = yaml.safe_load(_text(repo / taskfile)) or {}
            tasks = data.get("tasks", {}) if isinstance(data, dict) else {}
            candidates.append((taskfile, "task", set(tasks) if isinstance(tasks, dict) else set()))
        except yaml.YAMLError:
            pass
    for source, command, targets in candidates:
        setup_target = next((target for target in ("setup", "install", "bootstrap", "deps") if target in targets), "")
        test_target = "test" if "test" in targets else ""
        lint_target = "lint" if "lint" in targets else ""
        if setup_target or test_target or lint_target:
            return (
                f"{command} {setup_target}" if setup_target else "",
                f"{command} {test_target}" if test_target else "",
                f"{command} {lint_target}" if lint_target else "",
                [source],
            )
    return "", "", "", []


def _documented_commands(repo: Path, files: list[Path]) -> tuple[str, str, str, list[str]]:
    """Read explicit development commands before falling back to ecosystem guesses."""
    rels = {p.relative_to(repo).as_posix(): p for p in files}
    ordered = [name for name in ("AGENTS.md", "CONTRIBUTING.md", "README.md") if name in rels]
    ordered.extend(sorted(name for name in rels if name.startswith("docs/") and name.endswith(".md")))
    found: dict[str, tuple[str, str]] = {}
    patterns = {
        "setup": re.compile(r"(?:^|\s)(?:uv venv|uv sync|uv pip install|python(?:3)? -m pip install|npm (?:ci|install)|go mod download|cargo fetch|bundle install)"),
        "test": re.compile(r"(?:^|\s)(?:[^\s`]+/)?(?:python -m )?pytest(?:\s|$)|(?:^|\s)(?:npm test|go test|cargo test|bundle exec rake test)(?:\s|$)"),
        "lint": re.compile(r"(?:^|\s)(?:[^\s`]+/)?ruff check(?:\s|$)|(?:^|\s)(?:npm run lint|go vet|cargo clippy|pre-commit run)(?:\s|$)"),
    }
    for name in ordered:
        text = _text(rels[name])
        candidates = re.findall(r"`([^`\n]+)`", text)
        candidates.extend(line.strip() for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL) for line in block.splitlines())
        for candidate in candidates:
            command = candidate.strip().rstrip(".")
            if not command or not _safe_ci_command(command):
                continue
            for kind, pattern in patterns.items():
                if kind not in found and pattern.search(command):
                    found[kind] = (command, name)
        if len(found) == 3:
            break
    sources = list(dict.fromkeys(source for _, source in found.values()))
    setup = found.get("setup", ("", ""))[0]
    test = found.get("test", ("", ""))[0]
    lint = found.get("lint", ("", ""))[0]
    return setup, test, lint, sources


def _commands(repo: Path, files: list[Path]) -> tuple[str, str, str, list[str]]:
    names = {p.relative_to(repo).as_posix() for p in files}
    ci = "\n".join(_text(p) for p in files if p.relative_to(repo).as_posix().startswith(".github/workflows/"))
    documented = _documented_commands(repo, files)
    if documented[0] and documented[1] and documented[2]:
        return documented
    runner = _runner_commands(repo, names)
    if runner[3]:
        setup, test, lint, sources = runner
        if not lint and ".pre-commit-config.yaml" in names:
            lint = "pre-commit run --all-files"
            sources.append(".pre-commit-config.yaml")
        return setup, test, lint, sources
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
        ci_commands = [
            command for m in re.findall(r"^\s*-\s*run:\s*(.+)$", ci, re.MULTILINE)
            if (command := m.strip()) and _safe_ci_command(command)
        ]
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
    if ".pre-commit-config.yaml" in names or "Dockerfile" in names:
        return (
            "docker build ." if "Dockerfile" in names else "",
            "",
            "pre-commit run --all-files" if ".pre-commit-config.yaml" in names else "",
            [name for name in ("Dockerfile", ".pre-commit-config.yaml") if name in names],
        )
    return "", "", "", []


def _project_name(repo: Path, rels: dict[str, Path]) -> tuple[str, str]:
    """Prefer a manifest's stable project name over a checkout/worktree directory name."""
    if "pyproject.toml" in rels:
        try:
            project = tomllib.loads(_text(rels["pyproject.toml"])).get("project", {})
            if isinstance(project, dict) and project.get("name"):
                return str(project["name"]), "pyproject.toml project.name"
        except tomllib.TOMLDecodeError:
            pass
    if "package.json" in rels:
        try:
            package = json.loads(_text(rels["package.json"]))
            if isinstance(package, dict) and package.get("name"):
                return str(package["name"]), "package.json name"
        except json.JSONDecodeError:
            pass
    return repo.name, "repository directory"


def _individual_codeowners(codeowners: str) -> list[str]:
    """Return individual GitHub logins, never the owner portion of an org/team token."""
    authors: set[str] = set()
    for line in codeowners.splitlines():
        for token in line.split("#", 1)[0].split()[1:]:
            if not token.startswith("@") or "/" in token:
                continue
            login = token.removeprefix("@")
            if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", login):
                authors.add(login)
    return sorted(authors)


def _documented_conventions(repo: Path, rels: dict[str, Path]) -> list[str]:
    """Extract explicit working agreements from the project's contributor-facing docs."""
    categories = {
        "Formatting": ("format", "style", "indent", "prett", "black", "ruff", "eslint", "gofmt"),
        "Review": ("review", "pull request", "merge", "approval", "approve"),
        "Commits": ("commit", "changeset", "signed-off", "conventional commit"),
    }
    candidates = [
        rel for rel in rels
        if Path(rel).name.lower() in {"readme.md", "contributing.md", "agents.md", "codeowners"}
        or rel.lower().startswith(".github/pull_request_template/")
        or rel.lower().startswith(".github/issue_template/")
        or rel.lower() == ".github/pull_request_template.md"
    ]
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for rel in sorted(candidates):
        for raw_line in _text(rels[rel]).splitlines():
            line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#+\s+|>\s*)", "", raw_line).strip()
            line = re.sub(r"\s+", " ", line)
            if not line or line.startswith("<!--") or len(line) > 300:
                continue
            lower = line.lower()
            for label, keywords in categories.items():
                if any(keyword in lower for keyword in keywords) and (label, line) not in seen:
                    found.append(f"{label}: {line} (source: `{rel}`).")
                    seen.add((label, line))
    codeowners = next((rel for rel in candidates if Path(rel).name.lower() == "codeowners"), "")
    if codeowners:
        found.append(f"Review: Respect path ownership recorded in CODEOWNERS (source: `{codeowners}`).")
    return found


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
        if remote_head:
            base, base_source = remote_head.removeprefix("origin/"), "git remote HEAD"
        elif current:
            base, base_source = current, "current Git branch"
        else:
            base, base_source = "main", "default fallback"
    except OSError:
        base, base_source = "main", "default fallback"
    setup, test, lint, command_sources = _commands(repo, files)
    project_name, name_source = _project_name(repo, rels)
    result = ProjectDiscovery(project_name, base, setup, test, lint, name_source=name_source, base_branch_source=base_source)
    result.read.extend(command_sources)
    result.inferred.extend(
        f"{label} command from {', '.join(command_sources) or 'available project metadata'}: {value or 'not determined'}"
        for label, value in (("setup", setup), ("test", test), ("lint", lint))
    )

    for rel, path in rels.items():
        lower = path.name.lower().split(".")[0]
        is_doc = (
            lower in _DOC_NAMES
            or rel.startswith("docs/")
            or rel.startswith(".github/ISSUE_TEMPLATE/")
            or rel.startswith(".github/PULL_REQUEST_TEMPLATE/")
            or rel == ".github/pull_request_template.md"
        )
        if is_doc and path.name not in _SECRET_FILES:
            result.read.append(rel)
            text = _text(path)
            if lower in {"todo", "roadmap"}:
                result.backlog.extend(
                    (line.lstrip("- *").strip(), rel)
                    for line in text.splitlines() if line.lstrip().startswith(("-", "*"))
                )
        if path.name in _SECRET_FILES or path.name.startswith(".env."):
            result.configure_by_hand.append(f"Secret file {rel} exists; configure its values by hand (file not read).")

    for path in files:
        if path.suffix in _SOURCE_EXTENSIONS:
            result.read.append(path.relative_to(repo).as_posix())
            for match in re.finditer(r"\b(?:TODO|FIXME)(?:\(([^)]+)\))?[: ]+([^\n]+)", _text(path, 30_000)):
                owner = f" ({match.group(1)})" if match.group(1) else ""
                source = path.relative_to(repo).as_posix()
                result.backlog.append((f"{match.group(2).strip()}{owner} — {source}", source))

    workflows = [p for rel, p in rels.items() if rel.startswith(".github/workflows/")]
    result.read.extend(path.relative_to(repo).as_posix() for path in workflows)
    ci_text = "\n".join(_text(path) for path in workflows)
    for env_name in sorted(set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", ci_text))):
        result.configure_by_hand.append(f"Environment variable {env_name} is referenced by CI; configure by hand.")
    codeowners = next((p for rel, p in rels.items() if p.name == "CODEOWNERS"), None)
    if codeowners:
        result.trusted_authors = _individual_codeowners(_text(codeowners))
    result.module_map = sorted(
        p.name + ("/" if p.is_dir() else "") for p in repo.iterdir()
        if p.name not in _SKIP_DIRS and not p.name.startswith(".")
    )[:40]
    result.conventions = _documented_conventions(repo, rels) + [
        f"Use the project's discovered test command: `{test}`." if test else "Choose and document a test command.",
        f"Use the project's discovered lint command: `{lint}`." if lint else "Choose and document a lint command.",
        f"Target the `{base}` branch for changes.",
    ]
    environment_inputs = [
        rel for rel in rels
        if rel in _ENVIRONMENT_FILES
        or rel.startswith(".devcontainer/")
    ]
    result.read.extend(environment_inputs)
    if any(rel == ".pre-commit-config.yaml" for rel in environment_inputs):
        result.conventions.append("Run the configured pre-commit hooks before review.")
    if any(rel == "Dockerfile" or rel.startswith(".devcontainer/") for rel in environment_inputs):
        result.conventions.append("Preserve the project's container-based development environment.")
    result.inferred.append(f"project name `{project_name}` from {name_source}")
    result.inferred.append(f"base branch `{base}` from {base_source}")
    result.inferred.append("module map from top-level repository entries")
    result.read = sorted(set(result.read))
    return result


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "product"


def _prefix(name: str) -> str:
    letters = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", name) if part)
    return (letters or re.sub(r"[^A-Za-z]", "", name))[:4].upper() or "PRJ"


_PROVENANCE_STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of",
    "on", "or", "the", "this", "to", "with", "goal", "context", "acceptance", "criteria",
    "out", "scope", "task", "add", "implement", "create", "update", "project",
}


def _provenance_words(text: object) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(word) > 1 and word not in _PROVENANCE_STOP_WORDS
    }


def _backlog_provenance(item: dict[str, object], backlog: list[tuple[str, str]]) -> str:
    """Accept or repair provenance only when task text identifies one backlog source."""
    provenance = str(item.get("discovered_from") or "")
    source = provenance.removeprefix("onboard:") if provenance.startswith("onboard:") else ""
    known_sources = {backlog_source for _, backlog_source in backlog}
    claimed_source = source if source in known_sources else ""
    task_words = _provenance_words(f"{item.get('title', '')} {item.get('body', '')}")
    scores: list[tuple[int, str]] = []
    for backlog_item, backlog_source in backlog:
        if claimed_source and backlog_source != claimed_source:
            continue
        backlog_words = _provenance_words(backlog_item)
        overlap = len(task_words & backlog_words)
        # One shared word is too weak for a multi-word item ("endpoint", "docs", etc.).
        # A genuinely one-word backlog item can still be identified by that one word.
        if overlap >= min(2, len(backlog_words)):
            scores.append((overlap, backlog_source))
    best = max((score for score, _ in scores), default=0)
    matches = {candidate for score, candidate in scores if score == best and score > 0}
    if len(matches) == 1:
        return f"onboard:{matches.pop()}"
    raise ValueError(
        f"planner task {item.get('title')!r} has no unambiguous backlog provenance; "
        "its title or body must identify one backlog item and discovered_from must name that source"
    )


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
    product = _slug(info.name)
    cfg_path = garden / CONFIG_NAME
    existing_config = yaml.safe_load(cfg_path.read_text()) or {} if cfg_path.exists() else {}
    collisions = [
        path for path in (garden / product, garden / "principles" / f"10-{product}-conventions.md")
        if path.exists()
    ]
    if product in (existing_config.get("products") or {}) or collisions:
        targets = [f"product {product!r} in garden.yaml"] if product in (existing_config.get("products") or {}) else []
        targets.extend(str(path.relative_to(garden)) for path in collisions)
        raise ValueError("onboarding would overwrite an existing product: " + ", ".join(targets))

    # GitHub is optional enrichment of the deterministic local result. It happens only in
    # the end-to-end command and every successful query is recorded in the report.
    _add_github_metadata(repo, info)
    created = init_garden(garden, f"{info.name} garden")
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
        github = config.setdefault("github", {})
        github["trusted_authors"] = sorted(set(github.get("trusted_authors") or []) | set(info.trusted_authors))
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
    backlog = info.backlog[:50] or [("No existing backlog item was found; choose the first deliverable.", "manual-decision")]
    goals.write_text(
        "# phase-01 goals (draft)\n\n## Why this phase\n\nTurn the existing project backlog into the first garden-managed slice.\n\n"
        "## Goals\n\n" + "\n".join(f"{i}. {item} _(source: `{source}`)_" for i, (item, source) in enumerate(backlog, 1)) + "\n\n"
        "## Non-goals\n\n- Work not represented in the discovered backlog.\n\n## Definition of done\n\n- The approved first-phase tasks are complete.\n"
    )
    store.invalidate()
    guidance = (
        "All tasks are onboarding drafts. Use only backlog items stated in the goals. "
        "For each item add discovered_from as onboard:<source>, using its stated source."
    )
    items = parse_plan(planner(store, plan_prompt(store, product, "phase-01", extra=guidance)))
    provenances = [_backlog_provenance(item, backlog) for item in items]
    tasks = import_plan(store, product, "phase-01", items, status="draft")
    for task, provenance in zip(tasks, provenances, strict=False):
        task.discovered_from = provenance
        store.save(task)
        created.append(task.path)

    report = product_dir / "docs" / "onboarding.md"
    report.parent.mkdir(exist_ok=True)
    open_decisions = [
        "Confirm or edit the product purpose, users and module map.", "Approve, edit or cancel every draft task.",
        "Choose model tiers and phase budgets.", "Configure notifications and the automerge policy.",
        *info.configure_by_hand,
    ]
    derived = [
        f"product slug `{product}` from project name `{info.name}` ({info.name_source})",
        f"task id prefix `{_prefix(info.name)}` from project name `{info.name}` ({info.name_source})",
        f"configured repository `{configured_repo}` from the onboarding source",
        *info.inferred,
        *(f"trusted author @{author} from CODEOWNERS" for author in info.trusted_authors),
        *(f"setup environment name `{name}` from a CI secret reference (value was not read)" for name in env_names),
        *(f"principle from discovered project configuration: {item}" for item in info.conventions),
        *(f"backlog item from `{source}`: {item}" for item, source in info.backlog),
    ]
    report.write_text(
        "# Onboarding report (draft)\n\n## Read\n\n" + "\n".join(f"- `{p}`" for p in info.read) + "\n\n"
        "## Inferences and provenance\n\n" + "\n".join(f"- {item}" for item in derived) + "\n\n"
        "## Could not determine\n\n" + "\n".join(
            [f"- {label} command" for label, value in (("setup", info.setup_command), ("test", info.test_command), ("lint", info.lint_command)) if not value]
            + [f"- {item}" for item in info.unavailable]
        ) + "\n\n"
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
        repo_name = re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1]) or "product"
        checkout = Path(tmp) / repo_name
        proc = subprocess.run(["git", "clone", "--quiet", source, str(checkout)], capture_output=True, text=True, check=False)
        if proc.returncode:
            raise ValueError(f"could not clone {source}: {proc.stderr.strip()}")
        return onboard_project(checkout, garden, repo_value=source, planner=planner)
