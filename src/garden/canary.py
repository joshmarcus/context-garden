"""`garden canary`: check a freshly-pinned build before it is trusted with real PRs.

Three bugs merged in phase 03 with green tests and failed in the live loop within the hour:
the merge queue rotated heads because a force-push puts the real rollup into pending (CG-176),
deleting a merged parent's branch made real GitHub close the stacked child's PR (CG-173), and
a restart lost a review verdict the old process had reaped. The canary installs the given
build into a throwaway venv and drives it end to end against an in-memory GitHub that behaves
like the real one in exactly those ways: the scripted QA flows (the whole web loop), plus a
stacked-PR scenario (a merging parent must not orphan its child) and a merge-queue scenario (a
pending rollup after the pre-merge force-push must not rotate the head). It exits non-zero on
any failure. Run it before moving the pin (see the `garden-operate` skill).

No tokens: the workers are the QA worker (`qa/worker.py`), a real process that finishes in a
second. No network beyond the one `pip install` of the pinned build.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import Status
from .qa.sandbox import MemoryGitHub
from .store import Store
from .upgrade import git_ref

WORKER = Path(__file__).parent / "qa" / "worker.py"


@dataclass
class CanaryReport:
    """What a canary run found: one row per check, and whether the install itself succeeded."""

    out: Path
    results: list[dict[str, Any]] = field(default_factory=list)
    install_ok: bool = True
    install_error: str = ""

    @property
    def ok(self) -> bool:
        return self.install_ok and bool(self.results) and all(r.get("ok") for r in self.results)

    def summary(self) -> str:
        lines: list[str] = []
        if not self.install_ok:
            lines.append(f"canary: FAILED to install the build: {self.install_error}")
        for r in self.results:
            mark = "ok  " if r.get("ok") else "FAIL"
            detail = f" · {r['detail']}" if r.get("detail") else ""
            lines.append(f"  {mark} {r['name']}{detail}")
        head = "canary: every check passed" if self.ok else "canary: FAILED"
        return "\n".join([head, *lines, f"  run directory: {self.out}"])


# ---- driving a real scheduler over a throwaway garden -----------------------------------

def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=canary@example.com", "-c", "user.name=canary", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _make_scenario_garden(root: Path, tasks: list[dict[str, Any]], config: dict[str, Any]) -> Path:
    """A throwaway garden with a real git repo and a bare origin, the QA worker as its harness,
    and `tasks` seeded ready. `config` is merged into garden.yaml."""
    root = root.resolve()
    garden = root / "garden"
    repo = root / "repo"
    remote = root / "remote.git"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    subprocess.run(["git", "config", "user.email", "canary@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "canary"], cwd=repo, check=True)
    (repo / "README.md").write_text("# demo\n\nThe throwaway product `garden canary` drives.\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)

    garden.mkdir()
    worker = [sys.executable, str(WORKER)]
    cfg: dict[str, Any] = {
        "name": "canary",
        "runner": "local",
        "harness": "claude",
        "max_attempts": 2,
        "max_revisions": 2,
        "timeout_minutes": 2,
        "tick_interval": 1,
        "review": {"enabled": False},
        "github": {"draft_pr": False},
        "worker_env": {"pass": ["PYTHONPATH", "COVERAGE_*"]},
        "harnesses": {"claude": {"command": [*worker, "--model", "{model}"],
                                 "resume_command": [*worker, "--resume", "{session}"],
                                 "output": "claude-json", "resume": True,
                                 "models": {"easy": "c-easy", "medium": "c-medium", "hard": "c-hard"}}},
        "products": {"demo": {"repo": "../repo", "base_branch": "main", "id_prefix": "DM", "github": "canary/demo"}},
    }
    _deep_merge(cfg, config)
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    (garden / "principles").mkdir()
    (garden / "principles" / "00-index.md").write_text("# Digest\n\n- Keep the change small.\n")
    phase = garden / "demo" / "p1"
    (phase / "tasks").mkdir(parents=True)
    (phase / "specs").mkdir()
    (garden / "demo" / "product.md").write_text("# demo\n\nA demo product for `garden canary`.\n")
    (phase / "goals.md").write_text("# p1 goals\n\nDrive the loop end to end.\n")
    (phase / "specs" / "spec.md").write_text("# spec\n\nNothing to it.\n")
    for t in tasks:
        deps = ", ".join(t.get("depends_on") or [])
        (phase / "tasks" / f"{t['id']}-{t['slug']}.md").write_text(
            f"---\nid: {t['id']}\ntitle: {t['title']}\nstatus: ready\ndepends_on: [{deps}]\n"
            f"priority: {t.get('priority', 1)}\ndifficulty: {t.get('difficulty', 'easy')}\nreading: []\n"
            f"created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n---\n\n"
            f"## Goal\n\n{t['title']}.\n"
        )
    return garden


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _drive(sched: Any, cond: Callable[[], bool], *, timeout: float = 90.0, interval: float = 0.3) -> bool:
    """Tick the scheduler until `cond` holds or `timeout` elapses (workers run detached and
    finish within a second, so a tick reaps the previous tick's dispatch)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sched.tick()
        sched.store.invalidate()
        if cond():
            return True
        time.sleep(interval)
    return False


def _approve_in_state(sched: Any, task_id: str) -> None:
    """Stand in for an approving automated review (review is off in these scenarios): record the
    verdict the merge queue's gate needs. The queue sets `automerge_ready_at` itself once the
    gate passes (its one writer is `scheduler/queue.py`)."""
    st = sched.state.get(task_id)
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["last_review_run"] = f"rev-{task_id}"
    st["review_rounds"] = 1
    sched.state.save()


def _scheduler_for(garden: Path, github: MemoryGitHub) -> Any:
    from .scheduler import Scheduler

    return Scheduler(Store(garden), github=github)


# ---- the two scenarios ------------------------------------------------------------------

def _scenario_stacked(root: Path, log: Callable[[str], None]) -> dict[str, Any]:
    """A merging parent must not orphan its stacked child. The merge queue retargets every open
    stacked-child PR to the final base before deleting the parent's branch (CG-173); the fake
    GitHub closes a child whose base is a deleted branch, so a build that skips the retarget
    fails here."""
    name = "stacked child survives the parent's merge"
    garden = _make_scenario_garden(root, [
        {"id": "DM-001", "slug": "parent", "title": "Parent task", "depends_on": []},
        {"id": "DM-002", "slug": "child", "title": "Child task", "depends_on": ["DM-001"]},
    ], {"stack": True, "max_parallel": 2, "github": {"automerge": True}})
    gh = MemoryGitHub()
    sched = _scheduler_for(garden, gh)
    parent_branch = sched.store.task("DM-001").default_branch()
    child_branch = sched.store.task("DM-002").default_branch()

    if not _drive(sched, lambda: child_branch in gh.prs and gh.prs.get(child_branch)
                  and gh.prs[child_branch].base == parent_branch):
        return {"name": name, "ok": False, "detail": "the child PR never opened stacked on the parent's branch"}

    _approve_in_state(sched, "DM-001")
    gh.prs[parent_branch].mergeable = "MERGEABLE"
    gh.set_checks(parent_branch, "SUCCESS")

    if not _drive(sched, lambda: gh.prs[parent_branch].state == "MERGED"):
        return {"name": name, "ok": False, "detail": "the merge queue never merged the parent"}
    child = gh.prs[child_branch]
    if child.state != "OPEN":
        return {"name": name, "ok": False,
                "detail": f"the child PR is {child.state.lower()} after the parent merged (orphaned by the base deletion)"}
    if child.base != "main":
        return {"name": name, "ok": False,
                "detail": f"the child PR base is {child.base!r}, expected 'main' (not retargeted before deletion)"}
    return {"name": name, "ok": True, "detail": "child retargeted to main before the parent's branch was deleted"}


def _scenario_merge_queue(root: Path, log: Callable[[str], None]) -> dict[str, Any]:
    """The merge queue merges two approved PRs while the fake reports a real check latency: a
    freshly-pushed rollup is PENDING for a poll or two, so a build that merges on a stale-green
    or rotates the head would stall or merge the wrong thing here."""
    name = "merge queue merges through a pending rollup"
    garden = _make_scenario_garden(root, [
        {"id": "DM-001", "slug": "one", "title": "Task one", "depends_on": [], "priority": 1},
        {"id": "DM-002", "slug": "two", "title": "Task two", "depends_on": [], "priority": 2},
    ], {"stack": False, "max_parallel": 2, "github": {"automerge": True}})
    gh = MemoryGitHub()
    gh.check_latency = 2  # a fresh push stays PENDING for two polls before it turns green
    sched = _scheduler_for(garden, gh)
    ids = ("DM-001", "DM-002")
    branches = {t: sched.store.task(t).default_branch() for t in ids}

    if not _drive(sched, lambda: all(sched.store.task(t).status == Status.IN_REVIEW for t in ids)):
        return {"name": name, "ok": False, "detail": "the two PRs never both reached review"}
    for tid in ids:
        _approve_in_state(sched, tid)
        gh.prs[branches[tid]].mergeable = "MERGEABLE"
        gh.set_checks(branches[tid], "SUCCESS")  # arm PENDING for check_latency polls, then green

    if not _drive(sched, lambda: all(sched.store.task(t).status == Status.DONE for t in ids), timeout=120):
        statuses = {t: sched.store.task(t).status.value for t in ids}
        return {"name": name, "ok": False, "detail": f"not both merged: {statuses}"}
    merged = [gh.prs[branches[t]].state for t in ids]
    if merged != ["MERGED", "MERGED"]:
        return {"name": name, "ok": False, "detail": f"PR states {merged}"}
    numbers = ", ".join(f"#{gh.prs[branches[t]].number}" for t in ids)
    return {"name": name, "ok": True, "detail": f"both PRs merged ({numbers}) once their rollups settled"}


def run_scenarios(out: Path, log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Run the stacked-PR and merge-queue scenarios against the in-memory GitHub. Each returns
    a row `{name, ok, detail}`."""
    say = log or (lambda m: None)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for scenario in (_scenario_stacked, _scenario_merge_queue):
        sub = out / scenario.__name__.lstrip("_")
        say(f"scenario: {scenario.__name__} ...")
        try:
            rows.append(scenario(sub, say))
        except Exception as e:  # noqa: BLE001 - a scenario crash is a canary failure, not a traceback
            rows.append({"name": scenario.__name__, "ok": False, "detail": f"crashed: {e}"})
    return rows


# ---- self-check (runs in whichever interpreter is under test) ---------------------------

def self_check(out: Path, log: Callable[[str], None] | None = None) -> CanaryReport:
    """Drive the scripted QA flows and the two scenarios in this interpreter. When run from the
    throwaway venv this is the pinned build checking itself."""
    from .qa import run_qa

    say = log or (lambda m: None)
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    say("scripted QA flows ...")
    report = run_qa(out / "qa", scripted=True, log=say)
    results.append({"name": "scripted QA flows", "ok": report.ok, "detail": report.summary().splitlines()[0]})
    results.extend(run_scenarios(out / "scenarios", say))
    return CanaryReport(out=out, results=results)


# ---- install into a throwaway venv ------------------------------------------------------

def install_build(url: str, sha: str, venv_dir: Path, log: Callable[[str], None] | None = None) -> tuple[bool, Path, str]:
    """Create a throwaway venv and `pip install` the build at `sha` from `url` (a git URL or a
    local path). Returns (ok, the venv's python, combined pip output)."""
    import venv as _venv

    say = log or (lambda m: None)
    _venv.create(venv_dir, with_pip=True)
    py = venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    spec = f"context-garden @ {git_ref(url)}@{sha}"
    say(f"pip install {spec}")
    proc = subprocess.run([str(py), "-m", "pip", "install", spec], capture_output=True, text=True)
    return proc.returncode == 0, py, (proc.stdout + proc.stderr).strip()


def run_canary(
    sha: str = "",
    *,
    url: str = "",
    out: Path | None = None,
    keep: bool = False,
    skip_install: bool = False,
    log: Callable[[str], None] | None = None,
) -> CanaryReport:
    """Install the build at `sha` into a throwaway venv and run its own self-check, or — with
    `skip_install` (or no `sha`) — run the self-check against the current build in process."""
    say = log or (lambda m: None)
    out = out or Path(tempfile.mkdtemp(prefix="garden-canary-"))
    out.mkdir(parents=True, exist_ok=True)
    if skip_install or not sha:
        return self_check(out, say)
    if not url:
        return CanaryReport(out=out, install_ok=False, install_error="no install URL (pass --url or set the tool product's repo)")
    venv_dir = out / "venv"
    say(f"installing {sha[:12]} into a throwaway venv at {venv_dir}")
    ok, py, output = install_build(url, sha, venv_dir, say)
    (out / "install.log").write_text(output)
    if not ok:
        return CanaryReport(out=out, install_ok=False, install_error=f"pip install failed (see {out / 'install.log'})")
    say(f"running the pinned build's own canary self-check ({py})")
    self_out = out / "self"
    proc = subprocess.run([str(py), "-m", "garden", "canary", "--self-check", "--out", str(self_out)],
                          capture_output=True, text=True)
    (out / "self-check.log").write_text(proc.stdout + proc.stderr)
    say(proc.stdout.strip())
    if not keep:
        import shutil

        shutil.rmtree(venv_dir, ignore_errors=True)
    return CanaryReport(out=out, results=[{"name": "pinned build self-check", "ok": proc.returncode == 0,
                                           "detail": (proc.stdout.strip().splitlines() or [""])[0]}])
