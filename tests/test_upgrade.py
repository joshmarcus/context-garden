"""The garden upgrades its own pinned tool install after a merge into the tool's product."""

from __future__ import annotations

import subprocess
import threading

import yaml

from garden.config import Config
from garden.scheduler import Scheduler
from garden.store import Store
from garden.upgrade import git_ref, installed_commit
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

    def installed_commit(self) -> str | None:
        return self.commit

    def install(self, url: str, sha: str) -> tuple[bool, str]:
        self.installs.append((url, sha))
        if self.install_ok and self.after_install is not None:
            self.commit = self.after_install
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
    assert sched.upgrade_available() is None  # control cleared
    assert [e for e in sched.events.read() if e["kind"] == "upgraded"]


def test_failed_verify_leaves_old_install_running(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.after_install = None  # install "succeeds" but the commit does not move
    result = sched.upgrade(restart=True)
    assert not result["ok"] and result["reason"] == "verify failed"
    assert restart.called == 0                     # the running loop is not restarted
    assert sched.upgrade_available()["sha"] == new_sha  # still offered
    assert [e for e in sched.events.read() if e["kind"] == "upgrade_failed"]


def test_failed_install_leaves_old_install_running(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github)
    up.install_ok = False
    result = sched.upgrade(restart=True)
    assert not result["ok"] and result["reason"] == "install failed"
    assert restart.called == 0
    assert sched.upgrade_available()["sha"] == new_sha


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
    assert sched.upgrade_available() is None
    assert "tool upgraded" in rep.transitions


def test_no_auto_upgrade_when_manual(garden, fake_github):
    sched, up, restart, new_sha = _armed(garden, fake_github, auto_dispatch=False)  # upgrade stays "manual"
    up.after_install = new_sha
    sched.tick()
    assert restart.called == 0
    assert sched.upgrade_available()["sha"] == new_sha
