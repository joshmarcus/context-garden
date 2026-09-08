import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden.cli import app
from garden.personas import phase_brief
from garden.runs import RunStore
from garden.scheduler import State
from garden.scheduler.checkruns import _is_ui_path
from garden.scheduler.report import TickReport
from garden.store import Store
from garden.walkthrough import (
    COLOR_SCHEMES,
    NARROW_FRAME_HEIGHT,
    NARROW_OUTER_WIDTH,
    VIEWPORTS,
    NarrowViewportError,
    PageResult,
    PageSpec,
    WalkthroughResult,
    _narrow_frame,
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
    decision = next(pr for pr in result.pages if pr.spec.slug == "task-decision")
    assert decision.spec.url == "/tasks/DM-001"
    assert 'class="panel decision-card"' in (out / "task-decision.html").read_text()


def test_capture_includes_representative_decision_card_without_live_decision(garden, tmp_path):
    store = Store(garden)
    task = store.task("DM-001")
    state = State(store.config.garden_dir / "state.json")
    facts = state.get(task.id)
    for key in ("decision", "question", "needs_human"):
        facts.pop(key, None)
    state.save()

    result = capture(store, store.phase("demo", "p1"), tmp_path / "walkthrough", screenshots=False)

    decision = next(page for page in result.pages if page.spec.slug == "task-decision")
    assert decision.spec.url == "/tasks/DM-001?walkthrough=decision"
    assert 'class="panel decision-card"' in (tmp_path / "walkthrough" / "task-decision.html").read_text()


def test_capture_marks_empty_or_failed_documents(garden, monkeypatch, tmp_path):
    store = Store(garden)
    phase = store.phase("demo", "p1")
    monkeypatch.setattr("garden.walkthrough._fetch", lambda *_args: {"now": (200, "")})
    monkeypatch.setattr("garden.walkthrough.pages_for", lambda *_args: [PageSpec("now", "/", "Now", "", "")])
    result = capture(store, phase, tmp_path / "empty", screenshots=False)
    assert result.pages[0].note == "empty or unsuccessful document"


def test_pages_include_the_phase_and_a_task(garden):
    store = Store(garden)
    specs = pages_for(store, store.phase("demo", "p1"))
    urls = {s.url for s in specs}
    assert "/phases/demo/p1" in urls
    assert any(s.url.startswith("/tasks/") for s in specs)
    assert "/" in urls


def test_includes_costs_backlog_retro(garden):
    specs = pages_for(Store(garden), Store(garden).phase("demo", "p1"))
    urls = {s.url for s in specs}
    assert "/costs" in urls
    assert "/board?view=backlog" in urls
    assert "/phases/demo/p1/retro" in urls


def test_ui_path_detection():
    assert _is_ui_path("src/garden/web/templates/inbox.html")
    assert _is_ui_path("assets/site.css")
    assert not _is_ui_path("src/garden/model.py")
    assert VIEWPORTS == (1280, 390)
    assert COLOR_SCHEMES == ("light", "dark")
    assert NARROW_OUTER_WIDTH == 600
    assert NARROW_FRAME_HEIGHT == 5400


def test_narrow_frame_uses_a_390px_content_viewport():
    class Page:
        def __init__(self):
            self.wrapper = ""
            self.scripts = []

        def set_content(self, wrapper, **_kwargs):
            self.wrapper = wrapper

        def frame(self, **_kwargs):
            return self

        def locator(self, _selector):
            return self

        def evaluate(self, script, *_args):
            self.scripts.append(script)
            if "scrollHeight" in script:
                return {"clientWidth": 390, "scrollWidth": 390, "scrollHeight": 5400}
            return None

    page = Page()
    measurements = _narrow_frame(page, "http://localhost:8765/inbox")

    assert 'src="http://localhost:8765/inbox"' in page.wrapper
    assert 'width:390px;height:5400px;border:0' in page.wrapper
    assert any("clientWidth" in script and "scrollWidth" in script for script in page.scripts)
    assert measurements == {"clientWidth": 390, "scrollWidth": 390}


def test_narrow_frame_executes_measurement_in_chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - local hosts may lack system libraries
            pytest.skip(f"Chromium unavailable in this environment: {exc}")
        page = browser.new_page(viewport={"width": NARROW_OUTER_WIDTH, "height": 900})
        measurements = _narrow_frame(
            page,
            "data:text/html,<html><body style='margin:0;width:390px'>fixture</body></html>",
        )
        browser.close()

    assert measurements == {"clientWidth": 390, "scrollWidth": 390}


def test_narrow_frame_rejects_content_overflow_after_measuring_it():
    class Page:
        def set_content(self, _wrapper, **_kwargs):
            pass

        def frame(self, **_kwargs):
            return self

        def locator(self, _selector):
            return self

        def evaluate(self, script, *_args):
            if "scrollHeight" in script:
                return {"clientWidth": 390, "scrollWidth": 646, "scrollHeight": 5400}
            return None

    with pytest.raises(NarrowViewportError, match="scrollWidth 646"):
        _narrow_frame(Page(), "http://localhost:8765/runs")


def test_ui_check_produces_expected_screenshot_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: None)

    def screenshots(_url, specs, out, _log):
        for spec in specs:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    (out / f"{spec.slug}-{width}-{scheme}.png").write_bytes(b"png")
        evidence = [{"page": spec.slug, "action": "navigate", "viewport": width,
                     "color_scheme": scheme, "clientWidth": width, "scrollWidth": width}
                    for spec in specs for width in VIEWPORTS for scheme in COLOR_SCHEMES]
        return {spec.slug for spec in specs}, None, evidence

    monkeypatch.setattr("garden.walkthrough._screenshot", screenshots)
    result = _seeded_ui_capture(tmp_path / "ui")
    assert result["status"] == "pass"
    assert "1280/390" in result["summary"]
    assert len(result["interaction_evidence"]) == len(result["pages"]) * len(VIEWPORTS) * len(COLOR_SCHEMES)
    assert (tmp_path / "ui" / "now.html").exists()
    assert (tmp_path / "ui" / "board.html").exists()
    assert (tmp_path / "ui" / "task.html").exists()
    assert "task-decision" in result["pages"]
    assert 'class="panel decision-card"' in (tmp_path / "ui" / "task-decision.html").read_text()
    for slug in ("now", "inbox", "board", "task"):
        for width in VIEWPORTS:
            for scheme in COLOR_SCHEMES:
                assert (tmp_path / "ui" / f"{slug}-{width}-{scheme}.png").exists()


def test_scoped_ui_check_does_not_expand_to_decision_card(tmp_path, monkeypatch):
    """An Inbox-only check validates only the requested surface."""
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: None)

    def screenshots(_url, specs, out, _log):
        for spec in specs:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    (out / f"{spec.slug}-{width}-{scheme}.png").write_bytes(b"png")
        evidence = [{"page": spec.slug, "action": "navigate", "viewport": width,
                     "color_scheme": scheme, "clientWidth": width, "scrollWidth": width}
                    for spec in specs for width in VIEWPORTS for scheme in COLOR_SCHEMES]
        return {spec.slug for spec in specs}, None, evidence

    monkeypatch.setattr("garden.walkthrough._screenshot", screenshots)
    result = _seeded_ui_capture(tmp_path / "ui", ["inbox"])

    assert result["status"] == "pass"
    assert result["pages"] == ["inbox"]
    assert not (tmp_path / "ui" / "task-decision-1280-light.png").exists()


def test_ui_check_rejects_html_only_output_as_infrastructure_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: {
        "ready": False, "kind": "missing_libraries", "diagnostic": "libnss3.so is missing"})
    result = _seeded_ui_capture(tmp_path / "ui")
    assert result["status"] == "fail"
    assert result["failure_kind"] == "infrastructure"
    assert result["browser_failure_kind"] == "missing_libraries"
    assert "0/" in result["summary"]
    assert not any(path.endswith(".png") for path in result["captures"])


@pytest.mark.parametrize("kind", [
    "missing_playwright", "missing_executable", "missing_libraries", "launch_failure",
])
def test_ui_check_preserves_structured_browser_failure_kind(kind, tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: {
        "ready": False, "kind": kind, "diagnostic": f"{kind} diagnostic"})
    result = _seeded_ui_capture(tmp_path / kind)
    assert result["status"] == "fail"
    assert result["failure_kind"] == "infrastructure"
    assert result["browser_failure_kind"] == kind


def test_ui_check_rejects_pngs_without_executed_interaction_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: None)

    def screenshots(_url, specs, out, _log):
        for spec in specs:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    (out / f"{spec.slug}-{width}-{scheme}.png").write_bytes(b"png")
        return {spec.slug for spec in specs}, None, []

    monkeypatch.setattr("garden.walkthrough._screenshot", screenshots)
    result = _seeded_ui_capture(tmp_path / "ui")
    assert result["status"] == "fail"
    assert result["failure_kind"] == "product"
    assert "interaction/viewport evidence is incomplete" in result["summary"]


def test_ui_check_keeps_application_http_failure_blocking_when_browser_is_unavailable(tmp_path, monkeypatch):
    def failed_capture(_store, _phase, out_dir, **_kwargs):
        out_dir.mkdir(parents=True)
        decision = PageSpec("task-decision", "/tasks/DM-001", "Decision", "purpose", "look")
        failed = PageSpec("task", "/tasks/DM-001", "Task", "purpose", "look")
        (out_dir / "task-decision.html").write_text('<div class="panel decision-card">decision</div>')
        (out_dir / "task-decision.txt").write_text("decision")
        (out_dir / "task.html").write_text("application failure")
        (out_dir / "task.txt").write_text("application failure")
        return WalkthroughResult(
            out_dir=out_dir,
            pages=[PageResult(decision, 200, 48),
                   PageResult(failed, 500, 19, note="empty or unsuccessful document")],
            screenshots=False,
            browser_note="Chromium unavailable",
            browser_failure_kind="missing_executable",
        )

    monkeypatch.setattr("garden.walkthrough.capture", failed_capture)
    result = _seeded_ui_capture(tmp_path / "ui")

    assert result["status"] == "fail"
    assert result["failure_kind"] == "product"
    assert "task (HTTP 500)" in result["summary"]


def test_ui_check_fails_when_browser_cannot_capture(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: {
        "ready": False, "kind": "launch_failure", "diagnostic": "Chromium unavailable"})

    result = _seeded_ui_capture(tmp_path / "ui")

    assert result["status"] == "fail"
    assert "Chromium unavailable" in result["details"]


def test_ui_check_fails_when_a_color_capture_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("garden.walkthrough._prepare_browser", lambda: None)

    def screenshots(_url, specs, out, _log):
        for spec in specs:
            for width in VIEWPORTS:
                for scheme in COLOR_SCHEMES:
                    if not (spec.slug == "now" and scheme == "dark"):
                        (out / f"{spec.slug}-{width}-{scheme}.png").write_bytes(b"png")
        return {spec.slug for spec in specs}, None, []

    monkeypatch.setattr("garden.walkthrough._screenshot", screenshots)
    result = _seeded_ui_capture(tmp_path / "ui")

    assert result["status"] == "fail"
    assert "now-1280-dark.png" in result["details"]


def test_ui_check_launches_renderer_from_changed_worktree(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["cwd"], seen["env"] = argv, kwargs["cwd"], kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, '{"status":"pass","pages":["now"]}\n', "")

    monkeypatch.setattr("garden.walkthrough.subprocess.run", run)
    monkeypatch.setenv("PYTHONPATH", "/controller/worktrees/CG-428/src")
    result = ui_check({"worktree": str(worktree)}, {"out_dir": str(tmp_path / "captures")})
    assert result["status"] == "pass"
    assert seen["cwd"] == worktree
    assert seen["env"]["PYTHONPATH"] == str(worktree / "src")
    assert seen["argv"][1:3] == ["-m", "garden.walkthrough"]


def test_ui_check_keeps_missing_proposed_source_blocking_in_advisory_mode(tmp_path):
    result = ui_check(
        {"worktree": str(tmp_path / "missing-worktree")},
        {"out_dir": str(tmp_path / "captures"), "capture_infrastructure_policy": "advisory"},
    )

    assert result["status"] == "error"
    assert result["summary"] == "UI check worktree source is missing"
    assert "capture_infrastructure" not in result


def test_ui_check_marks_unavailable_capture_output_path_from_trusted_wrapper(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    output_path = tmp_path / "captures"
    output_path.mkdir()
    fallback = output_path / "task.html"
    fallback.write_text("<main>rendered fallback</main>")

    def unavailable(*_args, **_kwargs):
        raise PermissionError("capture filesystem is read-only")

    monkeypatch.setattr("garden.walkthrough.tempfile.NamedTemporaryFile", unavailable)

    result = ui_check(
        {"worktree": str(worktree)},
        {"out_dir": str(output_path), "capture_infrastructure_policy": "advisory"},
    )

    assert result["status"] == "error"
    assert result["summary"] == "UI capture output path is unavailable"
    assert result["capture_infrastructure"]["source"] == "garden.walkthrough:ui_check"
    assert result["capture_infrastructure"]["kind"] == "capture_path_unavailable"
    assert result["captures"] == [str(fallback)]


def test_ui_check_keeps_renderer_traceback_blocking_in_advisory_mode(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    monkeypatch.setattr("garden.walkthrough._probe_child", lambda: {
        "ready": False, "kind": "missing_executable", "diagnostic": "browser missing",
    })
    monkeypatch.setattr("garden.walkthrough.subprocess.run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, "", "Traceback: application render failed"))

    result = ui_check(
        {"worktree": str(worktree)},
        {"out_dir": str(tmp_path / "captures"), "capture_infrastructure_policy": "advisory"},
    )

    assert result["status"] == "error"
    assert "Traceback" in result["details"]
    assert "capture_infrastructure" not in result


def test_ui_check_trusts_its_own_browser_probe_not_child_classification(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    monkeypatch.setattr("garden.walkthrough._probe_child", lambda: {
        "ready": False, "kind": "missing_libraries", "diagnostic": "trusted libnss diagnostic",
    })
    child = {
        "status": "fail", "failure_kind": "infrastructure", "summary": "no PNGs",
        "capture_infrastructure": {"source": "worker", "kind": "fake", "diagnostic": "spoof"},
    }
    monkeypatch.setattr("garden.walkthrough.subprocess.run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, json.dumps(child), ""))

    result = ui_check(
        {"worktree": str(worktree)},
        {"out_dir": str(tmp_path / "captures"), "capture_infrastructure_policy": "advisory"},
    )

    assert result["status"] == "fail"
    assert result["capture_infrastructure"] == {
        "source": "garden.walkthrough:ui_check", "kind": "browser_unavailable",
        "diagnostic": "trusted libnss diagnostic", "browser_kind": "missing_libraries",
    }


def test_ui_check_classifies_clean_missing_result_as_return_infrastructure(tmp_path, monkeypatch):
    worktree = tmp_path / "proposed"
    (worktree / "src").mkdir(parents=True)
    monkeypatch.setattr("garden.walkthrough._probe_child", lambda: {"ready": True})
    monkeypatch.setattr("garden.walkthrough.subprocess.run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "renderer log without a JSON result", ""))

    result = ui_check(
        {"worktree": str(worktree)},
        {"out_dir": str(tmp_path / "captures"), "capture_infrastructure_policy": "advisory"},
    )

    assert result["status"] == "error"
    assert result["capture_infrastructure"]["kind"] == "capture_result_unavailable"


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
    assert _prepare_browser() is None
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]


def test_scheduler_leaves_ui_verification_to_reviewer_and_preserves_explicit_checks(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.title = "Tighten inbox layout"
    task.extra["visual_scope"] = {"behavior": "Tighter inbox layout"}
    worktree = sched.worktree_for(task)
    worktree.mkdir(parents=True, exist_ok=True)
    captured = []
    runner = sched.runner_for(task, "local")
    monkeypatch.setattr(runner, "start_checks", lambda _run, _cwd, payload: captured.append(payload))
    monkeypatch.setattr(sched, "runner_for", lambda *_args, **_kwargs: runner)

    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names",
                        lambda _worktree, _base: ["src/garden/web/templates/inbox.html"])
    run = sched._dispatch_check_run(
        task, worktree=worktree, branch="garden/test", base="main",
        specs=[{"name": "focused", "command": "true"}], stage="pre_pr", cont={}, rep=TickReport(),
    )
    assert captured[-1]["specs"] == [{"name": "focused", "command": "true"}]
    assert run.env_snapshot["validation_plan"]["pages"] == ["inbox"]
    assert run.env_snapshot["validation_plan"]["evidence_policy"] == "reviewer_judgment"

    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names",
                        lambda _worktree, _base: ["src/garden/model.py"])
    sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                              specs=[], stage="pre_pr", cont={}, rep=TickReport())
    assert captured[-1]["specs"] == []

    # A generic criterion can preserve milestone validation, but does not make this
    # backend-only PR capture the walkthrough inventory.
    task.extra["requires"] = ["captures"]
    sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                              specs=[], stage="pre_pr", cont={}, rep=TickReport())
    assert captured[-1]["specs"] == []

    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names",
                        lambda _worktree, _base: ["src/garden/web/static/site.css"])
    run = sched._dispatch_check_run(task, worktree=worktree, branch="garden/test", base="main",
                                    specs=[], stage="pre_pr", cont={}, rep=TickReport())
    assert captured[-1]["specs"] == []
    assert run.env_snapshot["validation_plan"]["pages"] == ["board", "inbox"]


def test_explicit_empty_ui_capture_selection_captures_no_pages(garden, tmp_path):
    store = Store(garden)
    result = capture(store, store.phase("demo", "p1"), tmp_path, screenshots=False, pages=[])

    assert result.pages == []


def test_html_to_text_strips_tags_and_scripts():
    txt = html_to_text("<style>x{}</style><h1>Title</h1><p>One</p><script>bad()</script><p>Two &amp; more</p>")
    assert "Title" in txt and "One" in txt and "Two & more" in txt
    assert "bad()" not in txt and "<" not in txt


def test_html_to_text_omits_hidden_panels_and_attributes():
    txt = html_to_text(
        '<main><h1 title="tasks&quot;-&gt;Plan phase">Visible</h1>'
        '<div hidden>Hidden attribute</div>'
        '<aside style="display: none">Display hidden</aside>'
        '<aside style="display: none !important">Important hidden</aside>'
        '<section aria-hidden="true">ARIA hidden</section></main>'
    )
    assert "Visible" in txt
    assert "Hidden attribute" not in txt
    assert "Display hidden" not in txt
    assert "Important hidden" not in txt
    assert "ARIA hidden" not in txt
    assert "tasks\"-&gt;Plan phase" not in txt
    assert "tasks\"->Plan phase" not in txt


def test_html_to_text_omits_stylesheet_hidden_panels():
    txt = html_to_text(
        '<style>.panel { display: none; } #secret { display:none !important; }</style>'
        '<div class="panel">Hidden by class</div><p id="secret">Hidden by id</p>'
        '<p>Visible</p>'
    )
    assert "Hidden by class" not in txt
    assert "Hidden by id" not in txt
    assert "Visible" in txt


def test_html_to_text_does_not_overmatch_unsupported_or_nested_selectors():
    txt = html_to_text(
        '<style>[hidden] { display:none } details:not([open]) > summary { display:none }</style>'
        '<p>Visible sibling</p><div hidden>Hidden attribute</div>'
        '<details open><summary>Visible summary</summary><p>Visible details</p></details>'
    )
    assert "Hidden attribute" not in txt
    assert "Visible sibling" in txt
    assert "Visible summary" in txt
    assert "Visible details" in txt


def test_html_to_text_respects_child_selector_combinators():
    txt = html_to_text(
        '<style>.outer > .target { display:none }</style>'
        '<div class="outer"><div class="intermediate"><p class="target">Visible text</p></div></div>'
    )
    assert "Visible text" in txt


def test_html_to_text_applies_later_display_rule():
    txt = html_to_text(
        '<style>.panel { display:none } .panel { display:block }</style>'
        '<div class="panel">Restored text</div>'
    )
    assert "Restored text" in txt


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


def test_ui_check_entrypoint_accepts_new_controller_page_argument(monkeypatch, capsys, tmp_path):
    import json

    import garden.walkthrough as walkthrough

    calls = []

    def capture(path, pages):
        calls.append((path, pages))
        return {"status": "pass", "out_dir": str(path)}

    monkeypatch.setattr(walkthrough, "_seeded_ui_capture", capture)
    for selection, expected in [([], []), (['["*"]'], ["*"])]:
        monkeypatch.setattr(walkthrough.sys, "argv", ["garden.walkthrough", "--ui-check", str(tmp_path), *selection])
        assert walkthrough._main() == 0
        assert json.loads(capsys.readouterr().out) == {"status": "pass", "out_dir": str(tmp_path)}
        assert calls[-1] == (tmp_path, expected)
