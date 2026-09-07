"""Replay the CG-340 onboarding demonstration in a disposable shell project.

This is compatibility evidence, not a substitute for an existing maintainer accepting a
change.  The fixture never contacts GitHub or a model and leaves a JSON audit record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import yaml

from garden.graph import validate
from garden.onboard import onboard_project
from garden.scheduler import Scheduler
from garden.store import Store


def run(*args: str, cwd: Path, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != expected:
        raise RuntimeError(
            f"{' '.join(args)} returned {result.returncode}, expected {expected}: "
            f"{result.stderr or result.stdout}"
        )
    return result


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def git(repo: Path, *args: str) -> str:
    return run(
        "git", "-c", "user.name=Fixture Maintainer", "-c",
        "user.email=fixture@example.invalid", *args, cwd=repo,
    ).stdout.strip()


def planner(_store: Store, _prompt: str) -> str:
    return json.dumps([{
        "title": "Reject blank names",
        "priority": 1,
        "estimate": "S",
        "difficulty": "easy",
        "depends_on": [],
        "reading": ["TODO.md", "greet.sh"],
        "discovered_from": "onboard:TODO.md",
        "body": (
            "## Goal\n\nReject blank names instead of printing an incomplete greeting.\n\n"
            "## Context\n\nThe existing TODO requests this behavior.\n\n"
            "## Acceptance criteria\n\n- [ ] A blank name exits non-zero and explains the error.\n"
            "- [ ] A supplied name still prints the greeting.\n"
        ),
    }])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def demonstrate(root: Path) -> dict[str, object]:
    project = root / "hello-shell"
    garden = root / "garden"
    project.mkdir(parents=True)
    write(project / "README.md", "# hello-shell\n\nA dependency-free greeting command.\n")
    write(project / "TODO.md", "# Backlog\n\n- Reject blank names with a useful error\n")
    write(project / "greet.sh", '#!/bin/sh\nprintf "Hello, %s!\\n" "$1"\n')
    write(project / "test.sh", '#!/bin/sh\nset -eu\n[ "$(./greet.sh Ada)" = "Hello, Ada!" ]\n')
    write(
        project / "Makefile",
        "setup:\n\tchmod +x greet.sh test.sh\n"
        "test:\n\t./test.sh\n"
        "lint:\n\tsh -n greet.sh test.sh\n",
    )
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "Initial shell project")

    onboard_project(project, garden, planner=planner)
    config_path = garden / "garden.yaml"
    generated_config_hash = sha256(config_path)
    store = Store(garden)
    task = store.product("hello-shell").phases[0].tasks[0]
    graph_problems = validate(store.tasks())
    approval_warning = Scheduler(store, read_only=True).approve(
        task, by="fixture maintainer", phase=store.product("hello-shell").phases[0]
    )
    approved_task = Store(garden).task(task.id)

    git(project, "switch", "-q", "-c", "reject-blank-names")
    write(
        project / "greet.sh",
        '#!/bin/sh\nset -eu\nif [ "$#" -eq 0 ] || [ -z "$1" ]; then\n'
        '  echo "name must not be blank" >&2\n  exit 2\nfi\nprintf "Hello, %s!\\n" "$1"\n',
    )
    write(
        project / "test.sh",
        '#!/bin/sh\nset -eu\n[ "$(./greet.sh Ada)" = "Hello, Ada!" ]\n'
        'if ./greet.sh "" >blank.out 2>blank.err; then\n  echo "blank name unexpectedly passed" >&2\n'
        '  exit 1\nfi\n[ "$(cat blank.err)" = "name must not be blank" ]\nrm blank.out blank.err\n',
    )
    setup = run("make", "setup", cwd=project)
    tests = run("make", "test", cwd=project)
    lint = run("make", "lint", cwd=project)
    behavior = run("./greet.sh", "", cwd=project, expected=2)
    git(project, "add", "greet.sh", "test.sh")
    diff = run("git", "diff", "--cached", "--check", cwd=project)
    changed = run("git", "diff", "--cached", "--name-only", cwd=project).stdout.splitlines()
    review_findings = []
    if changed != ["greet.sh", "test.sh"]:
        review_findings.append(f"unexpected changed files: {changed}")
    if behavior.stderr.strip() != "name must not be blank":
        review_findings.append("blank-name error was not useful")
    if review_findings:
        raise RuntimeError("scripted fixture review rejected the change: " + "; ".join(review_findings))
    git(project, "commit", "-q", "-m", "Reject blank names")
    change_sha = git(project, "rev-parse", "HEAD")
    git(project, "switch", "-q", "main")
    git(project, "merge", "-q", "--no-ff", "reject-blank-names", "-m", "Accept blank-name validation")
    final_test = run("make", "test", cwd=project)

    return {
        "classification": {"repeatable_compatibility": "PASS", "real_user_adoption": "UNPROVEN"},
        "fixture": {"kind": "disposable POSIX shell project", "path": "<temporary>/hello-shell"},
        "onboarding": {
            "generated_config_sha256": generated_config_hash,
            "config_unchanged_after_journey": sha256(config_path) == generated_config_hash,
            "setup": yaml.safe_load(config_path.read_text())["products"]["hello-shell"]["setup"],
            "graph_validation": graph_problems,
            "task": {"id": task.id, "title": task.title, "provenance": task.discovered_from},
            "approval_status": approved_task.status.value,
            "approval_warning": approval_warning,
        },
        "implementation": {"change_sha": change_sha, "changed_files": changed},
        "automated_checks": {
            "make setup": setup.returncode,
            "make test": tests.returncode,
            "make lint": lint.returncode,
            "git diff --cached --check": diff.returncode,
            "final make test on merged main": final_test.returncode,
        },
        "scripted_fixture_review": {"result": "accepted", "findings": review_findings},
        "unverified": [
            "No named existing project or authorized maintainer was supplied.",
            "No external maintainer reviewed or accepted this fixture change.",
            "This replay does not demonstrate a hosted pull request or real model execution.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="Write the JSON evidence to this path")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cg340-onboarding-") as tmp:
        evidence = demonstrate(Path(tmp))
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
