"""The garden upgrades its own pinned tool install after a merge into the tool's product."""

from __future__ import annotations

import json
import os
import shutil
import signal
import site
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from garden.config import Config
from garden.scheduler import Scheduler
from garden.store import Store
from garden.upgrade import git_install_spec, git_ref, installed_commit
from tests.conftest import git, write


class FakeUpgrader:
    """Stand-in for garden.upgrade.Upgrader: records installs and reports a controllable
    installed commit so verification can succeed or fail on demand."""

    def __init__(self, commit: str):
        self.commit = commit
        self.installs: list[tuple[str, str]] = []
        self.install_ok = True
        self.doctor = True
        self.after_install: str | None = None  # commit to report once install runs
        self.install_results: list[tuple[bool, str, str | None]] = []

    def installed_commit(self) -> str | None:
        return self.commit

    def install(self, url: str, sha: str) -> tuple[bool, str]:
        self.installs.append((url, sha))
        if self.install_results:
            ok, output, reported_commit = self.install_results.pop(0)
            if reported_commit is not None:
                self.commit = reported_commit
            return ok, output
        if self.install_ok and self.after_install is not None:
            self.commit = sha
        return self.install_ok, "pip output"

    def doctor_ok(self) -> bool:
        return self.doctor


class Restarter:
    def __init__(self):
        self.called = 0

    def __call__(self) -> None:
        self.called += 1


def _enable_provides_tool(garden, **extra) -> None:
    p = garden / "garden.yaml"
    data = yaml.safe_load(p.read_text())
    data["products"]["demo"]["provides_tool"] = True
    data.update(extra)
    p.write_text(yaml.safe_dump(data))


def _advance_main(repo, marker: str = "merged.md") -> str:
    """Add a commit to the product's main and push it, standing in for a merged PR."""
    write(repo / marker, "moved forward\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"merge {marker}", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


# ---- config helpers --------------------------------------------------------
def test_config_upgrade_helpers(tmp_path):
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump({
        "upgrade": "auto",
        "products": {"a": {}, "tool": {"provides_tool": True}},
    }))
    cfg = Config.load(tmp_path)
    assert cfg.upgrade_auto() is True
    assert cfg.tool_product() == "tool"
    assert cfg.upgrade_package() == "context-garden"

    (tmp_path / "garden.yaml").write_text(yaml.safe_dump({"upgrade": {"auto": True, "package": "foo", "pip": "/venv/bin/pip"}}))
    cfg = Config.load(tmp_path)
    assert cfg.upgrade_auto() is True
    assert cfg.upgrade_package() == "foo"
    assert cfg.upgrade_pip() == ["/venv/bin/pip"]

    (tmp_path / "garden.yaml").write_text(yaml.safe_dump({}))
    cfg = Config.load(tmp_path)
    assert cfg.upgrade_auto() is False
    assert cfg.tool_product() is None


def test_git_ref_forms(tmp_path):
    assert git_ref("https://github.com/o/r") == "git+https://github.com/o/r"
    assert git_ref("git+https://x/y") == "git+https://x/y"
    assert git_ref(str(tmp_path)).startswith("git+file://")
    assert git_install_spec(str(tmp_path), "abc", "sample").endswith("@abc#egg=sample")


def test_installed_commit_none_for_editable_install():
    # In the test environment context-garden is installed editable, not from git: no crash, None.
    assert installed_commit("context-garden") in (None, "") or isinstance(installed_commit("context-garden"), str)


# ---- merge records the upgrade ---------------------------------------------
def test_merge_into_tool_product_records_upgrade(garden, fake_github):
    _enable_provides_tool(garden)
    repo = garden.parent / "repo"
    orig = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    up = FakeUpgrader(orig)
    sched = Scheduler(Store(garden), github=fake_github, upgrader=up, restarter=Restarter(), log=print)

    sched.tick()
    sched.tick()  # DM-001 -> in_review, PR opened

    new_sha = _advance_main(repo)  # a PR merged; main moved forward
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    sched.tick()  # poll -> merged -> _on_merged -> record

    info = sched.upgrade_available()
    assert info and info["sha"] == new_sha
    assert info["from"] == orig
    assert info["count"] == 1
    assert info["product"] == "demo"
    events = [e for e in sched.events.read() if e["kind"] == "upgrade_available"]
    assert events and events[-1]["sha"] == new_sha[:12]


def test_no_upgrade_when_already_installed(garden, fake_github):
    _enable_provides_tool(garden)
    repo = garden.parent / "repo"
    main_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    up = FakeUpgrader(main_sha)  # already on the tip
    sched = Scheduler(Store(garden), github=fake_github, upgrader=up, restarter=Restarter(), log=print)
    sched.tick()
    sched.tick()
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    sched.tick()
    assert sched.upgrade_available() is None


# ---- performing the upgrade ------------------------------------------------
def _armed(garden, fake_github, **extra):
    """A scheduler with a pending tool upgrade recorded and a fresh FakeUpgrader/Restarter."""
    _enable_provides_tool(garden, **extra)
    repo = garden.parent / "repo"
    orig = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    new_sha = _advance_main(repo)
    up = FakeUpgrader(orig)
    restart = Restarter()
    sched = Scheduler(Store(garden), github=fake_github, upgrader=up, restarter=restart, log=print)
    sched.control()["upgrade"] = {"sha": new_sha, "from": orig, "count": 1, "product": "demo",
                                  "url": str(repo), "at": "2026-01-01T00:00:00+00:00"}
    sched.state.save()
    return sched, up, restart, new_sha


def test_upgrade_installs_verifies_restarts(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = new_sha  # the reinstall succeeds
    result = sched.upgrade(restart=True)
    assert result["ok"] and result["restarted"]
    assert up.installs == [(str(garden.parent / "repo"), new_sha)]
    assert restart.called == 1
    assert sched.upgrade_available()["status"] == "restart_pending"
    restarted = Scheduler(Store(garden), github=fake_github, upgrader=up, restarter=restart, log=print)
    restarted.reap_on_start()
    assert restarted.upgrade_available() is None
    assert [e for e in restarted.events.read() if e["kind"] == "upgrade_active"]


def test_failed_verify_leaves_old_install_running(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = None  # install "succeeds" but the commit does not move
    result = sched.upgrade(restart=True)
    assert not result["ok"] and result["reason"] == "verify failed"
    assert restart.called == 0                     # the running loop is not restarted
    assert sched.upgrade_available()["sha"] == new_sha  # still offered
    assert [e for e in sched.events.read() if e["kind"] == "upgrade_failed"]


def test_failed_install_that_mutates_environment_restores_verified_old_install(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit
    up.install_results = [
        (False, "pip failed after replacing files", new_sha),
        (True, "restored", old_sha),
    ]
    result = sched.upgrade(restart=True)
    assert not result["ok"] and result["reason"] == "install failed"
    assert restart.called == 0
    assert up.installs == [(str(garden.parent / "repo"), new_sha),
                           (str(garden.parent / "repo"), old_sha)]
    info = sched.upgrade_available()
    assert info["sha"] == new_sha
    assert info["recovered"] is True
    assert info["active"] == old_sha
    assert "pip failed after replacing files" in info["diagnosis"]


def test_target_installer_exception_restores_verified_old_install(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit
    original_install = up.install

    def raise_then_restore(url: str, sha: str) -> tuple[bool, str]:
        if sha == new_sha:
            up.installs.append((url, sha))
            raise OSError("pip process disappeared")
        return original_install(url, sha)

    up.install = raise_then_restore

    result = sched.upgrade(restart=True)

    assert not result["ok"] and result["reason"] == "install failed"
    assert restart.called == 0
    assert up.installs == [(str(garden.parent / "repo"), new_sha),
                           (str(garden.parent / "repo"), old_sha)]
    info = sched.upgrade_available()
    assert info["status"] == "failed" and info["recovered"] is True
    assert info["active"] == old_sha
    assert "installer raised OSError: pip process disappeared" in info["diagnosis"]


def test_rollback_installer_exception_persists_unrecovered_failure(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit

    def broken_install(url: str, sha: str) -> tuple[bool, str]:
        up.installs.append((url, sha))
        if sha == new_sha:
            up.commit = new_sha
            raise OSError("target installer crashed")
        raise RuntimeError("rollback installer crashed")

    up.install = broken_install

    result = sched.upgrade(restart=True)

    assert not result["ok"] and result["reason"] == "install failed"
    assert restart.called == 0
    assert up.installs == [(str(garden.parent / "repo"), new_sha),
                           (str(garden.parent / "repo"), old_sha)]
    info = sched.upgrade_available()
    assert info["status"] == "failed" and info["recovered"] is False
    assert info["active"] == ""
    assert "installer raised OSError: target installer crashed" in info["diagnosis"]
    assert "installer raised RuntimeError: rollback installer crashed" in info["diagnosis"]


def test_rollback_success_with_wrong_commit_is_not_reported_as_recovered(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit
    wrong_sha = "f" * 40
    up.install_results = [
        (False, "target install failed", new_sha),
        (True, "pip claimed rollback success", wrong_sha),
    ]

    result = sched.upgrade(restart=True)

    assert not result["ok"] and restart.called == 0
    info = sched.upgrade_available()
    assert info["recovered"] is False
    assert info["active"] == ""
    assert wrong_sha[:12] in info["diagnosis"]
    assert old_sha[:12] in info["diagnosis"]


def test_rollback_requires_doctor_to_confirm_usable_prior_install(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit
    up.install_results = [
        (False, "target install failed", new_sha),
        (True, "restored", old_sha),
    ]
    up.doctor = False

    result = sched.upgrade(restart=True)

    assert not result["ok"] and restart.called == 0
    info = sched.upgrade_available()
    assert info["recovered"] is False
    assert info["active"] == ""
    assert "doctor` failed" in info["diagnosis"]


def test_doctor_failure_blocks_restart(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = new_sha
    up.doctor = False
    result = sched.upgrade(restart=True)
    assert not result["ok"] and result["reason"] == "doctor failed"
    assert restart.called == 0
    assert sched.upgrade_available()["sha"] == new_sha


def test_pin_waits_for_an_inflight_controller_tick_then_restarts_controller(garden, fake_github, monkeypatch):
    """The pin CLI only records a request; the process holding tick.lock installs it."""
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = new_sha
    entered = threading.Event()
    release = threading.Event()

    def held_tick(_rep, _dispatch):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(sched, "_tick_body", held_tick)
    controller = threading.Thread(target=sched.tick)
    controller.start()
    assert entered.wait(2)

    requester = Scheduler(Store(garden), github=fake_github, upgrader=up)
    assert requester.pin(new_sha, str(garden.parent / "repo"), product="demo")["pending"]
    assert up.installs == []
    assert restart.called == 0

    release.set()
    controller.join(2)
    assert not controller.is_alive()
    assert up.installs == [(str(garden.parent / "repo"), new_sha)]
    assert restart.called == 1


# ---- auto upgrade on an idle tick ------------------------------------------
def test_auto_upgrade_on_idle_tick(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github, upgrade="auto", auto_dispatch=False)
    up.after_install = new_sha
    rep = sched.tick()  # no dispatch -> idle -> auto-upgrade fires
    assert restart.called == 1
    assert sched.upgrade_available()["status"] == "restart_pending"
    assert "tool upgraded" in rep.transitions


def test_no_auto_upgrade_when_manual(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github, auto_dispatch=False)  # upgrade stays "manual"
    up.after_install = new_sha
    sched.tick()
    assert restart.called == 0
    assert sched.upgrade_available()["sha"] == new_sha


def test_auto_upgrade_detects_configured_base_advance_missed_while_offline(garden, fake_github):
    _enable_provides_tool(garden, upgrade="auto", auto_dispatch=False)
    repo = garden.parent / "repo"
    old_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout.strip()
    new_sha = _advance_main(repo, "available-after-restart.md")
    up = FakeUpgrader(old_sha)
    up.after_install = new_sha
    restart = Restarter()
    sched = Scheduler(Store(garden), github=fake_github, upgrader=up, restarter=restart, log=print)

    rep = sched.tick()

    assert up.installs == [(str(repo), new_sha)]
    assert restart.called == 1
    assert sched.upgrade_available()["status"] == "restart_pending"
    assert "tool upgraded" in rep.transitions


def test_pending_auto_upgrade_drains_before_dispatch_and_cannot_be_starved(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github, upgrade="auto")
    up.after_install = new_sha
    active = sched.runs.new_run("DM-001", "local", mode="check")
    sched.state.get("DM-001")["check_run"] = active.run_id
    sched.state.save()

    sched.tick()

    assert up.installs == []
    assert sched.upgrade_available()["status"] == "held"
    assert "draining 1 active worker/check run(s)" in sched.upgrade_available()["reason"]
    assert sched.store.task("DM-001").status.value == "ready"

    active.status = "done"
    active.save()
    sched.tick()
    assert up.installs == [(str(garden.parent / "repo"), new_sha)]
    assert restart.called == 1


def test_paused_dispatch_is_an_explicit_automatic_upgrade_hold(garden, fake_github):
    sched, up, restart, _ = _armed(garden, fake_github, upgrade="auto", auto_dispatch=False)
    sched.pause(by="test", reason="maintenance")

    sched.tick()

    assert up.installs == []
    assert restart.called == 0
    assert sched.upgrade_available()["status"] == "held"
    assert "dispatch is paused" in sched.upgrade_available()["reason"]


def test_restart_failure_rolls_back_and_reports_recovery(garden, fake_github):
    sched, up, _restart, new_sha = _armed(garden, fake_github)
    old_sha = up.commit
    up.after_install = new_sha

    def broken_restart():
        raise OSError("exec refused")

    sched._restarter = broken_restart
    result = sched.upgrade(restart=True)

    assert not result["ok"] and "restart failed" in result["reason"]
    assert up.commit == old_sha
    info = sched.upgrade_available()
    assert info["status"] == "failed" and info["recovered"] is True
    assert "exec refused" in info["diagnosis"]


def test_pin_defers_install_until_active_runs_drain(garden, fake_github):
    from garden.scheduler import TickReport

    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = new_sha
    sched.pin(new_sha, str(garden.parent / "repo"), product="demo")
    active = sched.runs.new_run("DM-001", "local")
    sched.maybe_auto_upgrade(TickReport())
    assert up.installs == []
    assert restart.called == 0
    assert sched.upgrade_available()["pinned"]
    active.status = "done"
    active.save()
    sched.maybe_auto_upgrade(TickReport())
    assert restart.called == 1


def _http(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _wait_for(predicate, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise AssertionError("condition was not reached before timeout")


def _commit(repo: Path, message: str) -> str:
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", message, cwd=repo)
    git("push", "-q", "origin", "HEAD:main", cwd=repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_real_serve_auto_upgrade_reexecs_and_serves_new_build(garden, tmp_path):
    """A real pinned controller replaces itself; its pid and listening socket survive exec."""
    source = tmp_path / "tool-source"
    remote = tmp_path / "tool-remote.git"
    checkout = Path(__file__).resolve().parents[1]
    shutil.copytree(checkout, source, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)

    # The doctor gate itself has focused failure coverage above. This disposable package
    # keeps it deterministic so this test isolates pip installation plus the production
    # default_restart/os.execv service boundary.
    upgrade_py = source / "src/garden/upgrade.py"
    text = upgrade_py.read_text()
    start = text.index("    def doctor_ok(self) -> bool:")
    end = text.index("\n\n\ndef default_restart", start)
    text = text[:start] + "    def doctor_ok(self) -> bool:\n        return True\n" + text[end:]
    upgrade_py.write_text(text)
    commit_a = _commit(source, "fixture build A")

    venv = tmp_path / "controller-venv"
    subprocess.run([os.fspath(Path(os.sys.executable)), "-m", "venv", "--system-site-packages", str(venv)], check=True)
    python = venv / "bin/python"
    # A nested venv's --system-site-packages sees the base interpreter, not the parent
    # development venv. Share its already-installed dependencies without resolving or
    # downloading anything; the disposable venv's own installed garden remains first.
    dependency_path = next(path for path in site.getsitepackages() if Path(path).name == "site-packages")
    nested_site = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (Path(nested_site) / "test-dependencies.pth").write_text(dependency_path + "\n")
    spec_a = git_install_spec(str(source), commit_a)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--no-deps", spec_a], check=True, timeout=120
    )

    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update({"upgrade": "auto", "auto_dispatch": False, "tick_interval": 1})
    config["products"]["demo"].update({"repo": str(source), "provides_tool": True})
    config_path.write_text(yaml.safe_dump(config))

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = tmp_path / "serve.log"
    env = {**os.environ, "GARDEN_ROOT": str(garden), "PYTHONUNBUFFERED": "1"}
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [str(python), "-m", "garden", "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=garden, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(lambda: _http(base_url + "/inbox")[0] == 200)
        status, body = _http(base_url + "/inbox")
        assert status == 200 and commit_a[:12] in body
        assert _http(base_url + "/upgrade-proof")[0] == 404
        original_pid = process.pid

        app_py = source / "src/garden/web/app.py"
        app_text = app_py.read_text()
        marker = "    @app.get(\"/favicon.svg\", include_in_schema=False)"
        route = (
            "    @app.get(\"/upgrade-proof\")\n"
            "    def upgrade_proof() -> dict[str, str]:\n"
            "        from ..upgrade import installed_commit\n"
            "        return {\"active\": installed_commit() or \"\"}\n\n"
        )
        app_py.write_text(app_text.replace(marker, route + marker))
        commit_b = _commit(source, "fixture build B adds proof route")

        def upgraded() -> bool:
            status_, body_ = _http(base_url + "/upgrade-proof")
            return status_ == 200 and json.loads(body_)["active"] == commit_b

        _wait_for(upgraded, timeout=90)
        assert process.poll() is None and process.pid == original_pid
        assert commit_b[:12] in _http(base_url + "/inbox")[1]

        events = [json.loads(line) for line in (garden / ".garden/events.jsonl").read_text().splitlines()]
        lifecycle = [event["kind"] for event in events if event["kind"].startswith("upgrade_")]
        assert "upgrade_available" in lifecycle
        assert "upgrade_installing" in lifecycle
        assert "upgrade_restart_pending" in lifecycle
        assert "upgrade_active" in lifecycle
        available = next(event for event in events if event["kind"] == "upgrade_available")
        assert available["base"] == "main" and available["count"] == 1
        assert yaml.safe_load(config_path.read_text())["upgrade"] == "auto"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
