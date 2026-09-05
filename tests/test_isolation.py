"""Worktree isolation: find_root() refuses to escape .garden/, GARDEN_ROOT blocks workers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from garden.config import find_root


def _make_garden(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "garden.yaml").write_text(yaml.safe_dump({"name": "test"}))
    return path


def _probe_garden_root_env(ctx, spec):
    """A custom `python:` check that spawns a subprocess without building its own env,
    used by test_run_check_guards_custom_python_checks_too below."""
    proc = subprocess.run(
        ["python3", "-c", "import os; print(os.environ.get('GARDEN_ROOT', ''))"],
        capture_output=True, text=True, check=False,
    )
    return {"status": "pass", "summary": proc.stdout.strip(), "details": ""}


def test_find_root_normal(tmp_path):
    root = _make_garden(tmp_path / "g")
    sub = root / "some" / "sub"
    sub.mkdir(parents=True)
    assert find_root(sub) == root


def test_find_root_refuses_worktree(tmp_path):
    """find_root() from inside .garden/worktrees/<id> must not return the enclosing garden."""
    root = _make_garden(tmp_path / "g")
    wt = root / ".garden" / "worktrees" / "CG-001" / "src"
    wt.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match=r"inside its \.garden/"):
        find_root(wt)


def test_find_root_refuses_any_dotgarden_subpath(tmp_path):
    root = _make_garden(tmp_path / "g")
    inner = root / ".garden" / "runs" / "CG-001"
    inner.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match=r"inside its \.garden/"):
        find_root(inner)


def test_find_root_worktree_nested_garden_is_fine(tmp_path):
    """A garden.yaml inside the worktree itself (not the enclosing one) is safe to return."""
    outer = _make_garden(tmp_path / "g")
    wt = outer / ".garden" / "worktrees" / "CG-001"
    _make_garden(wt)  # the worktree has its own garden.yaml (e.g. self-hosted)
    sub = wt / "src"
    sub.mkdir(parents=True)
    # find_root from `sub` should find the garden.yaml in `wt`, not the outer one
    assert find_root(sub) == wt


def test_conftest_fixture_clears_ambient_env(tmp_path):
    """The autouse `_no_ambient_garden_root` fixture in tests/conftest.py must strip both
    GARDEN_ROOT and GARDEN_EXEC_ROOT before every test, so this suite behaves the same in a
    developer's shell, in CI and under the check runner (see garden.checks module docstring:
    a product's tests must not depend on the garden's environment variables).

    By the time a test body runs, the autouse fixture has already executed, so we can't
    plant the ambient vars from inside a test and observe the fixture clear them. Instead
    run a throwaway test *inside tests/* (so it picks up the real tests/conftest.py) in a
    subprocess with both vars set ambiently (as the check runner does), and assert the
    fixture cleared them there.
    """
    import os
    import subprocess
    import sys

    tests_dir = Path(__file__).parent
    probe = tests_dir / "test_ambient_env_probe.py"
    probe.write_text(
        "import os\n"
        "def test_probe():\n"
        "    assert 'GARDEN_ROOT' not in os.environ\n"
        "    assert 'GARDEN_EXEC_ROOT' not in os.environ\n"
    )
    try:
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)  # don't let the outer run's flags reselect/deselect the probe
        env["GARDEN_ROOT"] = str(tmp_path / "nonexistent")
        env["GARDEN_EXEC_ROOT"] = str(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(probe)],
            cwd=tests_dir.parent, env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        probe.unlink()


def test_garden_root_env_valid_is_ignored(tmp_path, monkeypatch):
    """GARDEN_ROOT pointing to a real garden is ignored; cwd walk finds the right root.

    GARDEN_ROOT is not a supported way to redirect the root: workers and check
    subprocesses only ever see it set to a non-existent sentinel (no_live_garden_root),
    so find_root() must not use a valid-looking GARDEN_ROOT as a redirect either.
    """
    root = _make_garden(tmp_path / "g")
    other = _make_garden(tmp_path / "other")
    monkeypatch.setenv("GARDEN_ROOT", str(other))
    # GARDEN_ROOT=other is ignored; find_root(root) still finds root via the cwd walk
    assert find_root(root) == root


def test_garden_root_env_invalid(tmp_path, monkeypatch):
    """GARDEN_ROOT set to a non-existent path must fail with a clear message."""
    monkeypatch.setenv("GARDEN_ROOT", str(tmp_path / "nonexistent"))
    with pytest.raises(FileNotFoundError, match="workers must not run garden"):
        find_root()


def test_local_runner_sets_garden_root(sched, monkeypatch):
    """The local runner must pass GARDEN_ROOT to workers pointing at a non-existent path.

    The real LocalRunner's launch is exercised with its Popen stubbed out: the worker's
    environment is captured at the launch call and no process is started."""
    import garden.runner.local as local_mod
    from garden.runner.local import LocalRunner

    launched: list[dict] = []

    def stub_popen(*args, **kwargs):
        launched.append(dict(kwargs["env"]))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(local_mod.subprocess, "Popen", stub_popen)
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local")
    LocalRunner({"setup": {}}, sched.cfg.harness("claude")).start(run, sched.store.root, "brief")

    assert launched, "no worker launch was intercepted"
    env = launched[0]
    assert env["GARDEN_TASK_ID"] == task.id and env["GARDEN_RUN_ID"] == run.run_id
    assert "GARDEN_ROOT" in env, "GARDEN_ROOT must be set in worker environment"
    # The sentinel path must not be a valid garden (no garden.yaml there)
    assert not (Path(env["GARDEN_ROOT"]) / "garden.yaml").exists(), (
        f"worker GARDEN_ROOT={env['GARDEN_ROOT']!r} must not point at a real garden"
    )


def test_check_ctx_exposes_exec_root_not_garden_root(sched):
    """check_ctx must carry the live garden's root under exec_root, never under a `root`
    key — the generic ctx-to-env mapping in checks.py would otherwise leak it as
    GARDEN_ROOT and defeat the sentinel that keeps check commands off the live garden."""
    task = sched.store.task("DM-001")
    ctx = sched.check_ctx(task, task.default_branch(), "main")
    assert ctx.get("exec_root") == str(sched.store.root)
    assert "root" not in ctx


def test_run_check_forces_garden_root_sentinel(sched, tmp_path):
    """A check command must see GARDEN_EXEC_ROOT for the live garden's own root, but
    GARDEN_ROOT must always be a non-existent sentinel, even though check_ctx's exec_root
    is a real garden — closing the dual-use CG-054 flagged as fragile."""
    from garden.checks import run_check

    ctx = sched.check_ctx(sched.store.task("DM-001"), "b", "main")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result = run_check(
        {
            "name": "env-probe",
            "command": (
                "python3 -c \"import os, json; "
                "print(json.dumps({'summary': os.environ.get('GARDEN_ROOT', ''), "
                "'details': os.environ.get('GARDEN_EXEC_ROOT', '')}))\""
            ),
        },
        ctx, cwd=worktree,
    )
    assert result["status"] == "pass"
    garden_root, garden_exec_root = result["summary"], result["details"]
    assert garden_exec_root == str(sched.store.root)
    assert garden_root != str(sched.store.root)
    assert not (Path(garden_root) / "garden.yaml").exists()


def test_run_check_guards_custom_python_checks_too(sched, tmp_path, monkeypatch):
    """A custom `python:` callable that spawns a subprocess without building its own env
    must still see the GARDEN_ROOT sentinel, not the scheduler's own environment —
    CG-082 review feedback: guard every python check, not only the built-in helper."""
    import os

    from garden.checks import run_check

    monkeypatch.setenv("GARDEN_ROOT", str(sched.store.root))  # simulate an unguarded caller
    ctx = sched.check_ctx(sched.store.task("DM-001"), "b", "main")
    result = run_check(
        {"name": "custom-probe", "python": "tests.test_isolation:_probe_garden_root_env"}, ctx, cwd=tmp_path
    )

    assert result["status"] == "pass"
    assert result["summary"] != str(sched.store.root)
    assert not (Path(result["summary"]) / "garden.yaml").exists()
    # the guard is scoped to the call: the caller's own env is restored afterwards
    assert os.environ["GARDEN_ROOT"] == str(sched.store.root)


# ---- the scrubbed worker environment (CG-154) --------------------------------


def test_scrubbed_env_keeps_the_allowlist_and_drops_the_rest(monkeypatch):
    from garden.runner.base import scrubbed_env

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GARDEN_ENV", "work")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    env = scrubbed_env({}, {"env": {"WIDGET_HOME": "/opt/widget"}}, worktree="/wt/T-1")
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "GARDEN_ENV", "CLAUDECODE"):
        assert name not in env, name
    assert env["ANTHROPIC_API_KEY"] == "sk-ant" and env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["LC_ALL"] == "C.UTF-8" and env["PATH"] == os.environ["PATH"]
    # HOME is not the operator's: it is an isolated scratch home beside the worktree, so the
    # worker cannot read ~/.config/gh, ~/.git-credentials or ~/.ssh.
    assert env["HOME"] != os.environ["HOME"]
    assert env["HOME"] == "/wt/.garden-home-T-1"
    assert env["WIDGET_HOME"] == "/opt/widget"  # setup.env rides on top
    # worker_env.pass adds names and globs; "*" restores full inheritance
    env = scrubbed_env({"worker_env": {"pass": ["AWS_*", "SSH_AUTH_SOCK"]}}, worktree="/wt/T-1")
    assert env["AWS_SECRET_ACCESS_KEY"] == "aws_secret" and env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert "GITHUB_TOKEN" not in env
    env = scrubbed_env({"worker_env": {"pass": ["*"]}}, worktree="/wt/T-1")
    assert env["GITHUB_TOKEN"] == "ghp_secret" and "CLAUDECODE" not in env
    assert env["HOME"] == os.environ["HOME"]  # "*" (or "HOME") restores the operator's home
    # naming HOME explicitly restores it too
    env = scrubbed_env({"worker_env": {"pass": ["HOME"]}}, worktree="/wt/T-1")
    assert env["HOME"] == os.environ["HOME"]


def test_scrubbed_env_carries_each_harness_config_dir_by_default(monkeypatch):
    """CG-217: an isolated worker HOME must not also hide a harness's saved login. Each
    harness's own config-dir variable is set to the *operator's* real home by default, even
    though HOME itself points at the isolated scratch home."""
    from garden.runner.base import scrubbed_env

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    env = scrubbed_env({}, worktree="/wt/T-1")
    assert env["HOME"] != os.environ["HOME"]  # the isolated scratch home, not the operator's
    assert env["CLAUDE_CONFIG_DIR"] == str(Path(os.environ["HOME"]) / ".claude")
    assert env["CODEX_HOME"] == str(Path(os.environ["HOME"]) / ".codex")

    # an operator-set value passes straight through (the CLAUDE_* / CODEX_* allowlist)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude-creds")
    env = scrubbed_env({}, worktree="/wt/T-1")
    assert env["CLAUDE_CONFIG_DIR"] == "/srv/claude-creds"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    # worker_env.config_dirs overrides the default, keyed by the variable name
    env = scrubbed_env({"worker_env": {"config_dirs": {"CLAUDE_CONFIG_DIR": "/opt/claude-creds",
                                                        "MY_HARNESS_HOME": "/opt/my-harness"}}},
                       worktree="/wt/T-1")
    assert env["CLAUDE_CONFIG_DIR"] == "/opt/claude-creds"
    assert env["CODEX_HOME"] == str(Path(os.environ["HOME"]) / ".codex")  # untouched default
    assert env["MY_HARNESS_HOME"] == "/opt/my-harness"  # a custom harness's own documented key


def test_run_setup_runs_in_the_scrubbed_env(tmp_path, monkeypatch):
    from garden.runner.base import run_setup

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    wt = tmp_path / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    out = tmp_path / "env.txt"
    run_setup(wt, {"command": f"env > {out}", "env": {"WIDGET_HOME": "/opt/widget"}})
    seen = out.read_text()
    assert "GITHUB_TOKEN" not in seen
    assert "ANTHROPIC_API_KEY=sk-ant" in seen and "WIDGET_HOME=/opt/widget" in seen


def test_run_check_command_scrubs_the_schedulers_credentials(tmp_path, monkeypatch):
    """CG-164: a `command` check runs code the branch itself wrote (its own test suite via
    the pre_pr `tests` default), so it must get the same scrubbed environment as the worker
    (runner.base.scrubbed_env) rather than the scheduler's own os.environ — no GitHub token
    or cloud credentials, even with no `config` passed in."""
    from garden.checks import run_check

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    result = run_check(
        {
            "name": "env-probe",
            "command": (
                "python3 -c \"import os, json; "
                "print(json.dumps({'summary': os.environ.get('GITHUB_TOKEN', ''), "
                "'details': os.environ.get('ANTHROPIC_API_KEY', '')}))\""
            ),
        },
        {"branch": "feat"}, cwd=tmp_path,
    )
    assert result["status"] == "pass"
    assert result["summary"] == ""  # GITHUB_TOKEN dropped
    assert result["details"] == "sk-ant"  # ANTHROPIC_* stays (the claude harness needs it)


def test_run_check_command_respects_worker_env_pass(tmp_path, monkeypatch):
    """`worker_env.pass` in garden.yaml applies to checks the same way it applies to the
    worker: pass the live config through as run_check's `config` argument to widen it."""
    from garden.checks import run_check

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    result = run_check(
        {
            "name": "env-probe",
            "command": "python3 -c \"import os, json; print(json.dumps({'summary': os.environ.get('GITHUB_TOKEN', '')}))\"",
        },
        {}, cwd=tmp_path, config={"worker_env": {"pass": ["GITHUB_TOKEN"]}},
    )
    assert result["status"] == "pass" and result["summary"] == "ghp_secret"


def test_local_worker_does_not_inherit_the_schedulers_credentials(sched, monkeypatch):
    """The worker process gets the scrubbed environment: the harness's own key and the
    product's setup env, but not the scheduler's GitHub token, cloud credentials or ssh
    agent; the run still works end to end. The environment is captured at the launch step,
    the one the in-process runner shares with the real local runner."""
    from tests.inprocess import InProcessRunner

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    captured: list[dict] = []
    orig_launch = InProcessRunner.launch

    def spy_launch(self, run, worktree, brief_path, env):
        captured.append(dict(env))
        return orig_launch(self, run, worktree, brief_path, env)

    monkeypatch.setattr(InProcessRunner, "launch", spy_launch)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]
    env = next(e for e in captured if "GARDEN_TASK_ID" in e)
    for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "CLAUDECODE"):
        assert name not in env, name
    assert env["ANTHROPIC_API_KEY"] == "sk-ant" and env["GARDEN_RUN_ID"]
    assert "GARDEN_ROOT" in env and not (Path(env["GARDEN_ROOT"]) / "garden.yaml").exists()
    # HOME is an isolated scratch home beside the worktree, never the operator's
    assert env["HOME"] != os.environ.get("HOME") and ".garden-home-" in env["HOME"]
    assert (sched.runs.latest("DM-001").path / "exit_code").read_text().strip() == "0"


def test_check_command_runs_under_an_isolated_home(tmp_path, monkeypatch):
    """CG-194: a `command` check runs the branch's own code, so HOME must be an isolated
    scratch home beside the worktree, not the operator's — a branch's test suite cannot read
    ~/.config/gh, ~/.git-credentials or ~/.ssh."""
    from garden.checks import run_check

    worktree = tmp_path / "worktrees" / "T-1"
    worktree.mkdir(parents=True)
    result = run_check(
        {"name": "home-probe",
         "command": "python3 -c \"import os, json; print(json.dumps({'summary': os.environ.get('HOME', '')}))\""},
        {"branch": "feat"}, cwd=worktree,
    )
    assert result["status"] == "pass"
    assert result["summary"] != os.environ.get("HOME")
    assert result["summary"] == str(tmp_path / "worktrees" / ".garden-home-T-1")


def test_check_command_home_restored_by_worker_env_pass(tmp_path, monkeypatch):
    """`worker_env.pass: [HOME]` restores the operator's home for checks, the escape hatch for
    a tool that needs it."""
    from garden.checks import run_check

    worktree = tmp_path / "worktrees" / "T-1"
    worktree.mkdir(parents=True)
    result = run_check(
        {"name": "home-probe",
         "command": "python3 -c \"import os, json; print(json.dumps({'summary': os.environ.get('HOME', '')}))\""},
        {"branch": "feat"}, cwd=worktree, config={"worker_env": {"pass": ["HOME"]}},
    )
    assert result["status"] == "pass" and result["summary"] == os.environ.get("HOME")
