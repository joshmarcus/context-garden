from __future__ import annotations

import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from garden.github import Feedback, PRInfo
from garden.scheduler import Scheduler
from garden.store import Store

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FAKE_SSH = Path(__file__).parent / "fake_ssh.py"


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


@pytest.fixture
def garden(tmp_path: Path) -> Path:
    """A garden with one product whose repo is a local git repo with a bare origin."""
    root = tmp_path / "garden"
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    write(repo / "README.md", "# demo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
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
        "harnesses": {
            "claude": {"bin": str(FAKE_CLAUDE), "max_turns": 5},
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
        self.feedback: dict[int, Feedback] = {}
        self._n = 100

    def describe(self):
        return "fake"

    def me(self):
        return "garden-bot"

    def find_pr(self, slug, head_branch):
        return self.prs.get(head_branch)

    def get_pr(self, slug, number):
        for pr in self.prs.values():
            if pr.number == number:
                return pr
        raise KeyError(number)

    def create_pr(self, slug, head, base, title, body, draft=False, reviewers=None):
        self._n += 1
        pr = PRInfo(number=self._n, url=f"https://example.com/pull/{self._n}", state="OPEN", title=title, head=head, base=base, updated_at="t1")
        self.prs[head] = pr
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        return pr

    def feedback_since(self, slug, number, since_iso, exclude_logins=None):
        return self.feedback.get(number, Feedback())

    def comment(self, slug, number, body):
        self.comments.append(body)

    def update_pr(self, slug, number, title="", body=""):
        for pr in self.prs.values():
            if pr.number == number:
                if title:
                    pr.title = title
                if body:
                    pr.body = body
                self.updated.append({"number": number, "title": title, "body": body})


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def sched(garden, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    store = Store(garden)
    return Scheduler(store, github=fake_github, log=print)


def wait_for_runs(sched: Scheduler, timeout: float = 15.0) -> None:
    """Block until every active detached run has written its exit_code (fake claude is fast)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        active = [r for r in sched.runs.active() if r.runner != "manual"]
        if all((r.path / "exit_code").exists() for r in active):
            return
        time.sleep(0.05)
    raise TimeoutError("fake workers did not finish")
