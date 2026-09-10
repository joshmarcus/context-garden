from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_timeout
import yaml

from garden import runner as runner_registry
from garden.github import Feedback, PRInfo
from garden.runner.base import _no_fsmonitor_env
from garden.scheduler import Scheduler
from garden.store import Store
from tests.inprocess import InProcessRunner

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FAKE_SSH = Path(__file__).parent / "fake_ssh.py"
_SESSION_STARTED_MONOTONIC = 0.0


def pytest_addoption(parser):
    parser.addoption(
        "--run-stress", action="store_true", default=False,
        help="Opt in to stress/load experiments (excluded from ordinary test runs and CI)",
    )
    parser.addoption(
        "--garden-shard-worker", action="store_true", default=False,
        help="Internal: execute one file shard of the macOS full suite",
    )


def pytest_configure(config):
    """Keep this suite from leaving a git filesystem-monitor daemon behind every repository.

    Where `core.fsmonitor` is on machine-wide, each fixture repository makes git start a
    `git fsmonitor--daemon`; it detaches, outlives the test that caused it and goes on
    watching a deleted temporary directory, so a full run accumulates hundreds of them.
    `GIT_CONFIG_*` is the highest-priority git configuration, so setting it on this process
    reaches every git the suite runs, including the ones product code runs on its behalf.
    """
    os.environ.update(_no_fsmonitor_env())
    if sys.platform == "darwin":
        # /usr/bin/git is a developer-tools shim on macOS. Resolve the same Apple Git
        # implementation once rather than paying the shim's lookup cost for every one of
        # the suite's thousands of short Git commands.
        try:
            resolved = subprocess.run(
                ["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True,
                check=False, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            resolved = ""
        if resolved and Path(resolved).is_file():
            os.environ["PATH"] = f"{Path(resolved).parent}:{os.environ.get('PATH', '')}"


def _full_suite_shards(root: Path, count: int = 3) -> list[list[str]]:
    """Balance test files by source size without collecting the suite a second time."""
    files = sorted((root / "tests").rglob("test_*.py"), key=lambda path: path.stat().st_size,
                   reverse=True)
    buckets: list[tuple[int, list[str]]] = [(0, []) for _ in range(min(count, len(files)))]
    for path in files:
        index = min(range(len(buckets)), key=lambda item: buckets[item][0])
        weight, members = buckets[index]
        members.append(str(path.relative_to(root)))
        buckets[index] = weight + path.stat().st_size, members
    return [members for _weight, members in buckets]


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """Run only the default macOS full suite as three independent file shards.

    macOS process and filesystem startup makes the otherwise 7-minute serial Linux suite
    exceed its unchanged 900-second guard. Focused selections and Linux/WSL stay serial.
    Each child retains pytest's 120-second per-test and 900-second session deadlines, while
    this parent applies the same monotonic 900-second bound to the complete set.
    """
    if sys.platform != "darwin" or config.getoption("garden_shard_worker"):
        return None
    if config.getoption("file_or_dir"):
        return None
    invocation = tuple(config.invocation_params.args)
    if any(argument not in {"-q", "--quiet"} for argument in invocation):
        return None

    root = Path(config.rootpath)
    shards = _full_suite_shards(root)
    timeout = float(config.getoption("session_timeout") or config.getini("session_timeout") or 0)
    deadline = time.monotonic() + timeout if timeout > 0 else None
    print(f"macOS full suite: {len(shards)} file shards (same tests and timeout bounds)")
    with tempfile.TemporaryDirectory(prefix="garden-pytest-shards-") as output_dir:
        processes: list[tuple[subprocess.Popen[bytes], Path, object]] = []
        for index, files in enumerate(shards, 1):
            output = Path(output_dir) / f"shard-{index}.log"
            handle = output.open("wb")
            command = [
                sys.executable, "-m", "pytest", *invocation, "--garden-shard-worker", *files,
            ]
            process = subprocess.Popen(
                command, cwd=root, env=dict(os.environ), stdout=handle,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            processes.append((process, output, handle))

        timed_out = False
        try:
            while any(process.poll() is None for process, _output, _handle in processes):
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    for process, _output, _handle in processes:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGTERM)
                    grace = time.monotonic() + 5
                    while (time.monotonic() < grace
                           and any(process.poll() is None for process, _output, _handle in processes)):
                        time.sleep(0.05)
                    for process, _output, _handle in processes:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(0.05)
        finally:
            for process, _output, handle in processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                handle.close()

        for index, (_process, output, _handle) in enumerate(processes, 1):
            print(f"\n--- macOS test shard {index}/{len(processes)} ---")
            print(output.read_text(errors="replace"), end="")
        if timed_out:
            print(f"macOS full-suite timeout: {timeout:g} seconds exceeded")
            return 1
        return 0 if all(process.returncode == 0 for process, _output, _handle in processes) else 1


def pytest_sessionstart(session):
    """Anchor the unchanged session budget to a clock immune to wall-clock corrections."""
    global _SESSION_STARTED_MONOTONIC
    _SESSION_STARTED_MONOTONIC = time.monotonic()


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_protocol(item):
    """Keep pytest-timeout's wall-clock session guard aligned with monotonic elapsed time.

    pytest-timeout 2.4 stores its session expiry as a `time.time()` deadline. A clock
    correction can therefore end a new suite hundreds of seconds early. This inner hook
    runs before the plugin evaluates that deadline after each test and re-anchors its
    existing 900-second budget; the plugin still owns and reports the limit.
    """
    yield
    configured = item.config.getoption("session_timeout") or item.config.getini("session_timeout")
    timeout = float(configured or 0)
    if timeout <= 0 or _SESSION_STARTED_MONOTONIC <= 0:
        return
    remaining = timeout - (time.monotonic() - _SESSION_STARTED_MONOTONIC)
    item.config.stash[pytest_timeout.SESSION_EXPIRE_KEY] = time.time() + remaining


def pytest_collection_modifyitems(config, items):
    """Deselect stress before fixtures run, even for explicit node or -m selections."""
    if config.getoption("--run-stress"):
        return
    selected = []
    deselected = []
    for item in items:
        target = deselected if item.get_closest_marker("stress") is not None else selected
        target.append(item)
    if deselected:
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


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
    """Strip live-garden and execution identity inherited from the process environment.

    This keeps the suite identical in a developer shell, CI, and a supervised validation.
    Tests of either guard or nested execution set the relevant values explicitly.

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
    monkeypatch.delenv("GARDEN_EXECUTION_LEASED", raising=False)
    monkeypatch.delenv("GARDEN_EXECUTION_OWNER", raising=False)
    monkeypatch.delenv("GARDEN_EXECUTION_RUN_DIR", raising=False)
    monkeypatch.delenv("GARDEN_HEAVY_EXECUTION", raising=False)
    monkeypatch.delenv("GARDEN_OWNER_SCOPED", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_harness_config(monkeypatch):
    """Tests must not read a developer's real Claude or Codex configuration."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def git(*args: str, cwd: Path, timeout: float = 30) -> None:
    """Run a fixture Git command with a bounded wait and no leaked pipe holders.

    Fixture repositories intentionally preserve their files after failures for pytest's
    diagnostics.  A timed-out Git process is different: its process group can retain the
    captured stderr pipe after Git itself exits, which otherwise leaves pytest waiting
    indefinitely during setup.
    """
    command = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args]
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.output or ""
        partial_stderr = exc.stderr or ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode(errors="replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode(errors="replace")
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - the suite's supported CI hosts are POSIX.
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - the suite's supported CI hosts are POSIX.
                process.kill()
            stdout, stderr = process.communicate()
        if partial_stdout and not stdout.startswith(partial_stdout):
            stdout = partial_stdout + stdout
        if partial_stderr and not stderr.startswith(partial_stderr):
            stderr = partial_stderr + stderr
        raise RuntimeError(
            f"fixture git command timed out after {timeout:.1f}s: {' '.join(command)}\n"
            f"stderr:\n{stderr}"
        ) from exc
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)


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
    if not t.reading:
        t.reading = ["demo/p1/specs/spec.md"]
    store.save(t)


@pytest.fixture(scope="session")
def garden_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build the immutable starting Git topology once for the test session."""
    template_root = tmp_path_factory.mktemp("garden-template")
    repo = template_root / "repo"
    remote = template_root / "remote.git"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
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
    return repo, remote


@pytest.fixture
def garden(tmp_path: Path, garden_template: tuple[Path, Path]) -> Path:
    """A garden with independently mutable copies of one seeded Git topology."""
    root = tmp_path / "garden"
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    template_repo, template_remote = garden_template
    # Clone from the immutable session seed instead of copying every loose Git file for
    # every test.  A shared clone shares only immutable objects through an alternate;
    # refs, config, the index and worktree files remain independent.  This distinction is
    # material on filesystems where thousands of small-file copies dominate suite time.
    subprocess.run(["git", "clone", "-q", "--shared", str(template_repo), str(repo)], check=True)
    subprocess.run([
        "git", "clone", "-q", "--bare", "--shared", str(template_remote), str(remote),
    ], check=True)
    git("remote", "set-url", "origin", str(remote), cwd=repo)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    root.mkdir()
    (root / "garden.yaml").write_text(yaml.safe_dump({
        "name": "test",
        "max_attempts": 2,
        "max_revisions": 2,
        "revision_policy": {"enabled": False, "every": 2, "decision_after": 6},
        "max_parallel": 2,
        # Most tests are about other scheduler responsibilities. Storage-admission tests
        # explicitly enable the production 20 GiB default with deterministic probes.
        "resources": {"disk_reserve_bytes": 0},
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


@pytest.fixture(autouse=True)
def _remote_clone_for_ssh_tests(request: pytest.FixtureRequest) -> None:
    """Create the remote-host checkout only for tests that execute the SSH runner.

    Most tests need the product repository and its bare origin for ordinary worker and
    Git behavior, but do not use the second clone that represents an SSH host.  The
    marker keeps that topology available to the integration cases without paying for it
    in every garden fixture.
    """
    if request.node.get_closest_marker("needs_remote_clone") is None:
        return
    garden = request.getfixturevalue("garden")
    remote = garden.parent / "remote.git"
    clone = garden.parent / "remote-clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)


class FakeGitHub:
    """Scriptable stand-in for garden.github.GitHub."""

    def __init__(self):
        self.available = True
        self.remote: Path | None = None
        self.prs: dict[str, PRInfo] = {}  # branch -> PR
        self.created: list[dict] = []
        self.comments: list[str] = []
        self.updated: list[dict] = []
        self.closed: list[int] = []
        self.readied: list[int] = []
        self.merged: list[dict] = []
        self.feedback: dict[int, Feedback] = {}
        self.complete_feedback_snapshots: dict[int, dict] = {}
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

    def find_open_pr(self, slug, head_branch):
        pr = self.prs.get(head_branch)
        return pr if pr is not None and pr.state == "OPEN" else None

    def find_open_pr_by_base(self, slug, base_branch):
        return next((pr for pr in self.prs.values()
                     if pr.state == "OPEN" and pr.base == base_branch), None)

    def list_open_prs(self, slug, project_users=None):
        authors = {self.me(), *(project_users or [])}
        return [
            # Provider reads return fresh value objects. Keep repository observation
            # refreshes from exposing the mutable object held by the fake's backend.
            replace(self.get_pr(slug, pr.number))
            for pr in self.prs.values()
            if pr.state == "OPEN" and (pr.author or self.me()) in authors
        ]

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
                if self.remote is not None:
                    found = subprocess.run(
                        ["git", "ls-remote", str(self.remote), f"refs/heads/{pr.head}"],
                        capture_output=True, text=True, check=False,
                    ).stdout.split()
                    pr.head_sha = found[0] if found else ""
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
        pr = PRInfo(number=self._n, url=f"https://example.com/pull/{self._n}", state="OPEN", title=title, head=head, base=base, body=body, updated_at="t1", is_draft=draft, author=self.me())
        self.prs[head] = pr
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        if self.check_latency > 0:  # a fresh push starts CI: PENDING until it settles
            self.set_checks(head, "SUCCESS")
        return pr

    def feedback_since(self, slug, number, since_iso, exclude_logins=None):
        return self.feedback.get(number, Feedback())

    def incremental_feedback_since(self, slug, number, since_iso, exclude_logins=None):
        return self.feedback_since(slug, number, since_iso, exclude_logins)

    def complete_feedback(self, slug, number):
        return self.complete_feedback_snapshots.get(number, {
            "repository": slug, "pr": number, "fetched_at": "2026-01-01T00:00:00+00:00",
            "complete": True, "errors": [], "items": [],
        })

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

    def merge_pr(self, slug, number, method="squash", delete_branch=True, expected_head=""):
        for pr in self.prs.values():
            if pr.number == number:
                if expected_head and pr.head_sha != expected_head:
                    from garden.github import GitHubError
                    raise GitHubError("head changed before merge")
                pr.state = "MERGED"
                self.merged.append({"number": number, "method": method,
                                    "delete_branch": delete_branch})
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
