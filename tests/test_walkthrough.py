import os
from pathlib import Path

from typer.testing import CliRunner

from garden.cli import app
from garden.personas import phase_brief
from garden.store import Store
from garden.walkthrough import capture, html_to_text, newest_walkthrough, pages_for


def _run(garden, *args):
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_capture_writes_pages_and_index(garden):
    store = Store(garden)
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


def test_pages_include_the_phase_and_a_task(garden):
    store = Store(garden)
    specs = pages_for(store, store.phase("demo", "p1"))
    urls = {s.url for s in specs}
    assert "/phases/demo/p1" in urls
    assert any(s.url.startswith("/tasks/") for s in specs)


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
    from tests.conftest import FakeGitHub, wait_for_runs

    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    wait_for_runs(sched)
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
