from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from garden import runner as runner_registry
from garden.github import Feedback, PRInfo
from garden.scheduler import Scheduler
from garden.store import Store
from tests.inprocess import InProcessRunner

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FAKE_SSH = Path(__file__).parent / "fake_ssh.py"


@pytest.fixture(autouse=True)
def in_process_workers(monkeypatch):
    """No test drives a subprocess worker: for the whole suite the `local` runner (and its
    `claude-local` alias) is the in-process one from tests/inprocess.py, so every Scheduler
    a test builds, directly or through the web app, CLI or TUI, runs the fake harness
    synchronously inside `dispatch()`. A test that needs the real LocalRunner's launch
    mechanics constructs `LocalRunner` itself and stubs `subprocess.Popen`."""
    monkeypatch.setitem(runner_registry.REGISTRY, "local", InProcessRunner)
    monkeypatch.setitem(runner_registry.REGISTRY, "claude-local", InProcessRunner)


@pytest.fixture(autouse=True)
def _no_ambient_garden_root(monkeypatch):
    """Strip any GARDEN_ROOT / GARDEN_EXEC_ROOT inherited from the process environment
    before each test, so this suite passes the same way in a developer's shell, in CI and
    under the check runner (see garden.checks module docstring: a product's tests must not
    depend on the garden's environment variables).

    When this suite itself runs as the pre-PR `tests` check (see garden.checks.run_check),
    the check runner sets GARDEN_ROOT in the subprocess environment to a non-existent
    sentinel so a check command can't act on the live garden, and sets GARDEN_EXEC_ROOT to
    the live garden's own root. Both leak into every test in this process; GARDEN_ROOT makes
    find_root() raise regardless of cwd, breaking any test that calls find_root() (directly,
    or via Store(root=None)) without first managing GARDEN_ROOT itself. Tests that exercise
    either guard set it explicitly via monkeypatch, which layers on top of this baseline.
    """
    monkeypatch.delenv("GARDEN_ROOT", raising=False)
    monkeypatch.delenv("GARDEN_EXEC_ROOT", raising=False)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def complete_brief(garden_root: Path, task_id: str) -> None:
    """Replace a draft task's placeholder acceptance criteria with a real, testable one so
    `approve` accepts it (CG-193 refuses placeholder criteria). Used by tests that create a
    task from the `new-task` template and then need it approved."""
    store = Store(garden_root)
    t = store.task(task_id)
    if "## Acceptance criteria" not in t.body:
        t.body = t.body.rstrip() + "\n\n## Acceptance criteria\n\n- [ ] ...\n"
    t.body = t.body.replace("- [ ] ...", "- [ ] The thing works and is covered by a test.", 1)
    store.save(t)


@pytest.fixture
def garden(tmp_path: Path) -> Path:
    """A garden with one product whose repo is a local git repo with a bare origin."""
    root = tmp_path / "garden"
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    # set local user config so git-rebase can create commits without a global config
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    write(repo / "README.md", "# demo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    # pytest's numbered temporary-directory cleanup can remove an older sibling while
    # this fixture is being assembled. Ensure the bare remote's parent still exists
    # immediately before Git creates it.
    remote.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    # a second clone standing in for a remote host's checkout (ssh runner tests)
    subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / "remote-clone")], check=True)

    root.mkdir()
    (root / "garden.yaml").write_text(yaml.safe_dump({
        "name": "test",
        "max_attempts": 2,
        "max_revisions": 2,
        "max_parallel": 2,
        "timeout_minutes": 1,
        "review": {"enabled": False},
        "github": {"draft_pr": False},  # most tests exercise the non-draft flow; test_triage covers drafts
        # Workers run in a scrubbed environment (runner.base.scrubbed_env); the fake harness picks
        # its scenario from FAKE_CLAUDE_* and needs the interpreter's PYTHONPATH/coverage hooks.
        "worker_env": {"pass": ["FAKE_CLAUDE_*", "PYTHONPATH", "COVERAGE_*"]},
        "harnesses": {
            "claude": {"bin": str(FAKE_CLAUDE), "max_turns": {"easy": 40, "medium": 5, "hard": 80}},
            "codex": {"bin": str(FAKE_CODEX), "models": {"easy": "gpt-mini", "medium": "gpt-std", "hard": "gpt-max"}},
        },
        "ssh": {"ssh_bin": str(FAKE_SSH), "options": [],
                "hosts": [{"name": "boxA", "host": "boxA", "repos": {"demo": str(tmp_path / "remote-clone")}, "max_parallel": 1}]},
        "products": {"demo": {"repo": "../repo", "base_branch": "main", "id_prefix": "DM", "github": "test/demo"}},
    }))
    write(root / "principles" / "00-index.md", "# Digest\n\n- be good\n")
    write(root / "demo" / "product.md", "# demo\n\nA demo product.\n")
    write(root / "demo" / "p1" / "goals.md", "# p1\n\nShip it.\n")
    write(root / "demo" / "p1" / "specs" / "spec.md", "# spec\n\nDetails.\n")
    write(root / "demo" / "p1" / "tasks" / "DM-001-first.md", """
        ---
        id: DM-001
        title: First task
        status: ready
        depends_on: []
        priority: 1
        reading: [demo/p1/specs/spec.md]
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the first thing.
        """)
    write(root / "demo" / "p1" / "tasks" / "DM-002-second.md", """
        ---
        id: DM-002
        title: Second task
        status: ready
        depends_on: [DM-001]
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the second thing.
        """)
    return root


class FakeGitHub:
    """Scriptable stand-in for garden.github.GitHub."""

    def __init__(self):
        self.available = True
        self.prs: dict[str, PRInfo] = {}  # branch -> PR
        self.created: list[dict] = []
        self.comments: list[str] = []
        self.updated: list[dict] = []
        self.closed: list[int] = []
        self.readied: list[int] = []
        self.merged: list[dict] = []
        self.feedback: dict[int, Feedback] = {}
        self.reopened: list[int] = []
        self.deleted_branches: set[str] = set()  # branches GitHub has deleted
        self.base_deleted: set[int] = set()  # PR numbers with a base_ref_deleted timeline event
        self.refuse_reopen: set[int] = set()  # PR numbers GitHub refuses to reopen
        # Check latency (like real GitHub): after a push the rollup is PENDING for a few polls
        # before it turns green or red. `check_latency` is the default number of `get_pr` polls
        # a freshly-pushed rollup stays PENDING; `set_checks` arms a specific target and latency.
        self.check_latency = 0
        self._check_pending: dict[int, int] = {}  # PR number -> polls left before the rollup settles
        self._check_target: dict[int, str] = {}   # PR number -> the state the rollup settles to
        self._n = 100

    def describe(self):
        return "fake"

    def me(self):
        return "garden-bot"

    def is_authenticated(self):
        return True

    def find_pr(self, slug, head_branch):
        return self.prs.get(head_branch)

    def set_checks(self, branch, state, latency=None):
        """Arm a PR's checks rollup the way a push does on real GitHub: report PENDING for
        `latency` polls (default `check_latency`), then settle to `state` (SUCCESS/FAILURE).
        Tests use this to simulate a force-push restarting CI."""
        pr = self.prs[branch]
        n = self.check_latency if latency is None else latency
        self._check_target[pr.number] = state
        self._check_pending[pr.number] = n
        pr.checks = "PENDING" if n > 0 else state

    def get_pr(self, slug, number):
        for pr in self.prs.values():
            if pr.number == number:
                left = self._check_pending.get(number, 0)
                if left > 0:
                    self._check_pending[number] = left - 1
                    pr.checks = "PENDING"
                elif number in self._check_target:
                    pr.checks = self._check_target[number]
                return pr
        raise KeyError(number)

    def create_pr(self, slug, head, base, title, body, draft=False, reviewers=None):
        self._n += 1
        pr = PRInfo(number=self._n, url=f"https://example.com/pull/{self._n}", state="OPEN", title=title, head=head, base=base, body=body, updated_at="t1", is_draft=draft)
        self.prs[head] = pr
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        if self.check_latency > 0:  # a fresh push starts CI: PENDING until it settles
            self.set_checks(head, "SUCCESS")
        return pr

    def feedback_since(self, slug, number, since_iso, exclude_logins=None):
        return self.feedback.get(number, Feedback())

    def comment(self, slug, number, body):
        self.comments.append(body)

    def issue_comments(self, slug, number):
        return list(self.comments)

    def mark_ready(self, slug, number):
        for pr in self.prs.values():
            if pr.number == number:
                pr.is_draft = False
                self.readied.append(number)

    def close_pr(self, slug, number):
        for pr in self.prs.values():
            if pr.number == number:
                pr.state = "CLOSED"
                self.closed.append(number)

    def merge_pr(self, slug, number, method="squash", delete_branch=True):
        for pr in self.prs.values():
            if pr.number == number:
                pr.state = "MERGED"
                self.merged.append({"number": number, "method": method, "delete_branch": delete_branch})
                if delete_branch:
                    self.delete_branch(slug, pr.head)
                return
        raise KeyError(number)

    def delete_branch(self, slug, branch):
        """Delete a branch the way real GitHub does on merge: every open PR still targeting it
        is closed with a `base_ref_deleted` timeline event (the incident behind CG-173)."""
        self.deleted_branches.add(branch)
        for child in self.prs.values():
            if child.base == branch and child.state == "OPEN":
                child.state = "CLOSED"
                self.base_deleted.add(child.number)

    def reopen_pr(self, slug, number):
        from garden.github import GitHubError
        if number in self.refuse_reopen:
            raise GitHubError("cannot reopen: base branch was deleted")
        for pr in self.prs.values():
            if pr.number == number:
                pr.state = "OPEN"
                self.reopened.append(number)
                return
        raise KeyError(number)

    def branch_exists(self, slug, branch):
        return branch not in self.deleted_branches

    def base_ref_deleted(self, slug, number):
        return number in self.base_deleted

    def update_pr(self, slug, number, title="", body="", base=""):
        for pr in self.prs.values():
            if pr.number == number:
                if title:
                    pr.title = title
                if body:
                    pr.body = body
                if base:
                    pr.base = base
                self.updated.append({"number": number, "title": title, "body": body, "base": base})


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def sched(garden, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    store = Store(garden)
    return Scheduler(store, github=fake_github, log=print)
