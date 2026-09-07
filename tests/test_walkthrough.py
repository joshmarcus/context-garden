import os
import subprocess
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

from garden.cli import app
from garden.personas import phase_brief
from garden.runs import RunStore
from garden.scheduler.checkruns import _is_ui_path
from garden.scheduler.report import TickReport
from garden.store import Store
from garden.walkthrough import (
    COLOR_SCHEMES,
    VIEWPORTS,
    _prepare_browser,
    _redact_home,
    _scrub_stderr,
    _seeded_ui_capture,
    capture,
    html_to_text,
    newest_walkthrough,
    pages_for,
    ui_check,
)


def _run(garden, *args):
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_capture_writes_pages_and_index(garden):
    from garden.model import Status

    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.FAILED
    task.body += "\n## Log\n\n- a long failed-worker reason that the Inbox must keep readable\n"
    store.save(task)
    run = RunStore(store.config.garden_dir).new_run(
        task.id, "local", run_id="20260906T010102Z-revise-with-an-unusually-long-suffix"
    )
    run.status = "failed"
    run.error = "A long failure detail verifies the Inbox capture includes the decision card."
    run.save()
    ph = store.phase("demo", "p1")
    out = Path(garden) / "demo" / "p1" / "docs" / "walkthrough" / "2026-09-05"
    result = capture(store, ph, out, screenshots=False)

    # Every expected page is fetched with a 200 and written as both html and txt.
    slugs = {pr.spec.slug for pr in result.pages}
    assert {"inbox", "board", "board-list", "trellis", "phase", "task", "runs",
            "herbarium", "config", "trials", "events"} <= slugs
    for pr in result.pages:
        assert pr.status == 200, pr.spec.url
        assert (out / f"{pr.spec.slug}.html").exists()
        assert (out / f"{pr.spec.slug}.txt").exists()
    index = (out / "index.md").read_text()
    assert "Walkthrough of the live web app" in index
    assert "Screenshots were not captured" in index
    assert "`/board`" in index and "`/trellis`" in index
    inbox_capture = (out / "inbox.html").read_text()
    assert run.run_id in inbox_capture
    assert 'class="decision-evidence"' in inbox_capture
    assert 'class="card-actions decision-actions"' in inbox_capture


def test_pages_include_the_phase_and_a_task(garden):
    store = Store(garden)
    specs = pages_for(store, store.phase("demo", "p1"))
    urls = {s.url for s in specs}
    assert "/phases/demo/p1" in urls
    assert any(s.url.startswith("/tasks/") for s in specs)
    assert "/" in urls


def test_ui_path_detection():
    assert _is_ui_path("src/garden/web/templates/inbox.html")
    assert _is_ui_path("assets/site.css")
    assert not _is_ui_path("src/garden/model.py")
    assert VIEWPORTS == (1280, 390)
    assert COLOR_SCHEMES == ("light", "dark")


def test_ui_check_produces_expected_screenshot_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: "")

    def screenshots(_url, specs, out, _log):
        for spec in specs:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    (out / f"{spec.slug}-{width}-{scheme}.png").write_bytes(b"png")
        return {spec.slug for spec in specs}, ""

    monkeypatch.setattr("garden.walkthrough._screenshot", screenshots)
    result = _seeded_ui_capture(tmp_path / "ui")
    assert result["status"] == "pass"
    assert "1280/390" in result["summary"]
    assert (tmp_path / "ui" / "now.html").exists()
    assert (tmp_path / "ui" / "board.html").exists()
    assert (tmp_path / "ui" / "task.html").exists()
    for slug in ("now", "inbox", "board", "task"):
        for width in VIEWPORTS:
            for scheme in COLOR_SCHEMES:
                assert (tmp_path / "ui" / f"{slug}-{width}-{scheme}.png").exists()


def test_ui_check_launches_renderer_from_changed_worktree(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["cwd"], seen["env"] = argv, kwargs["cwd"], kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, '{"status":"pass","pages":["now"]}\n', "")

    monkeypatch.setattr("garden.walkthrough.subprocess.run", run)
    result = ui_check({"worktree": str(worktree)}, {"out_dir": str(tmp_path / "captures")})
    assert result["status"] == "pass"
    assert seen["cwd"] == worktree
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(worktree / "src")
    assert seen["argv"][1:3] == ["-m", "garden.walkthrough"]


def test_browser_is_prepared_automatically(monkeypatch):
    class Chromium:
        def launch(self):
            raise RuntimeError("browser executable missing")

    class Playwright:
        chromium = Chromium()

    class Context:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    calls = []
    package = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr("garden.walkthrough.subprocess.run", lambda argv, **_kwargs: (
        calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")))
    assert _prepare_browser() == ""
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]


def test_scheduler_adds_ui_check_for_ui_changes_or_required_captures(sched, monkeypatch):
    task = sched.store.task("DM-001")
    worktree = sched.worktree_for(task)
    worktree.mkdir(parents=True, exist_ok=True)
    captured = []
    runner = sched.runner_for(task, "local")
    monkeypatch.setattr(runner, "start_checks", lambda _run, _cwd, payload: captured.append(payload))
    monkeypatch.setattr(sched, "runner_for", lambda *_args, **_kwargs: runner)

    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names",
                        lambda _worktree, _base: ["src/garden/web/templates/inbox.html"])
    sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                              specs=[], stage="pre_pr", cont={}, rep=TickReport())
    ui = next(spec for spec in captured[-1]["specs"] if spec.get("name") == "ui")
    assert ui["worktree"] == str(worktree)
    assert "garden_root" not in ui

    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names",
                        lambda _worktree, _base: ["src/garden/model.py"])
    sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                              specs=[], stage="pre_pr", cont={}, rep=TickReport())
    assert not any(spec.get("name") == "ui" for spec in captured[-1]["specs"])

    task.extra["requires"] = ["captures"]
    sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                              specs=[], stage="pre_pr", cont={}, rep=TickReport())
    assert any(spec.get("name") == "ui" for spec in captured[-1]["specs"])


def test_html_to_text_strips_tags_and_scripts():
    txt = html_to_text("<style>x{}</style><h1>Title</h1><p>One</p><script>bad()</script><p>Two &amp; more</p>")
    assert "Title" in txt and "One" in txt and "Two & more" in txt
    assert "bad()" not in txt and "<" not in txt


def test_persona_phase_brief_includes_newest_walkthrough(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    prs = [{"id": "DM-001", "title": "First", "status": "done", "pr": "", "body": "b"}]

    # No walkthrough yet: the brief does not mention one.
    assert "Walkthrough of the live web app" not in phase_brief(store, ph, "designer", "main", prs)

    # Two dated captures: the brief points at (and inlines) the newest.
    root = ph.path / "docs" / "walkthrough"
    (root / "2026-09-04").mkdir(parents=True)
    (root / "2026-09-04" / "index.md").write_text("# old walkthrough\n")
    (root / "2026-09-05").mkdir(parents=True)
    (root / "2026-09-05" / "index.md").write_text("# new walkthrough\n\nboard etc.\n")
    assert newest_walkthrough(ph).name == "2026-09-05"

    brief = phase_brief(store, ph, "designer", "main", prs)
    assert "Walkthrough of the live web app" in brief
    assert "new walkthrough" in brief
    assert "old walkthrough" not in brief
    assert str(root / "2026-09-05") in brief


def test_capture_includes_a_run_page_when_a_task_has_run(garden):
    from garden.scheduler import Scheduler
    from tests.conftest import FakeGitHub

    # The in-process runner finishes the worker during dispatch, so the first tick
    # dispatches the run and the second reaps it; nothing needs to wait in between.
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()

    store = Store(garden)
    ph = store.phase("demo", "p1")
    out = Path(garden) / "cap"
    result = capture(store, ph, out, screenshots=False)
    run_pages = [pr for pr in result.pages if pr.spec.slug == "run"]
    assert run_pages and run_pages[0].status == 200
    assert (out / "run.html").exists()


def test_walkthrough_cli_writes_default_dir(garden):
    r = _run(garden, "walkthrough", "demo/p1", "--no-screenshots")
    assert r.exit_code == 0, r.output
    dirs = list((Path(garden) / "demo" / "p1" / "docs" / "walkthrough").iterdir())
    assert len(dirs) == 1
    assert (dirs[0] / "index.md").exists()
    assert (dirs[0] / "board.html").exists()


# --------------------------------------------------------------------------- hygiene: stderr + paths
def test_scrub_stderr_replaces_only_the_stderr_tab():
    page = ('<div class="tab-panel" data-tab="final"><pre class="log">ok</pre></div>'
            '<div class="tab-panel" data-tab="stderr"><pre class="log">'
            'Traceback: /home/josh/secret\nAPI_KEY=abc123</pre></div>')
    out = _scrub_stderr(page)
    assert "API_KEY" not in out and "Traceback" not in out
    assert "include-stderr" in out
    assert '<div class="tab-panel" data-tab="final"><pre class="log">ok</pre></div>' in out


def test_redact_home_replaces_every_occurrence():
    text = "brief at /home/josh/work/checkout/task.md, log at /home/josh/work/checkout/run.log"
    out = _redact_home(text, "/home/josh")
    assert "/home/josh" not in out
    assert out.count("~") == 2
    # no home configured (e.g. root user, HOME="/"): left alone rather than mangled
    assert _redact_home(text, "") == text
    assert _redact_home(text, "/") == text


def test_capture_omits_run_stderr_by_default_and_includes_it_on_request(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from tests.conftest import FakeGitHub

    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()

    store = Store(garden)
    ph = store.phase("demo", "p1")
    run = RunStore(store.config.garden_dir).runs_for("DM-001")[-1]
    (run.path / "stderr.log").write_text("Traceback (most recent call last):\nAPI_KEY=super-secret\n")

    default = capture(store, ph, Path(garden) / "cap-default", screenshots=False)
    run_html = (default.out_dir / "run.html").read_text()
    run_txt = (default.out_dir / "run.txt").read_text()
    assert "API_KEY" not in run_html and "API_KEY" not in run_txt
    assert "include-stderr" in run_html
    assert "stderr is omitted" in (default.out_dir / "index.md").read_text()

    included = capture(store, ph, Path(garden) / "cap-included", screenshots=False, include_stderr=True)
    assert "API_KEY" in (included.out_dir / "run.html").read_text()


def test_capture_redacts_the_home_directory(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from tests.conftest import FakeGitHub

    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()

    store = Store(garden)
    ph = store.phase("demo", "p1")
    run = RunStore(store.config.garden_dir).runs_for("DM-001")[-1]
    fake_home = "/home/fakeuser"
    (run.path / "final.md").write_text(f"wrote {fake_home}/work/checkout/src/thing.py")
    monkeypatch.setattr("garden.walkthrough.Path.home", staticmethod(lambda: Path(fake_home)))

    out = Path(garden) / "cap-redact"
    capture(store, ph, out, screenshots=False)
    run_html = (out / "run.html").read_text()
    assert fake_home not in run_html
    assert "~/work/checkout/src/thing.py" in run_html
    assert "paths are redacted" in (out / "index.md").read_text()
