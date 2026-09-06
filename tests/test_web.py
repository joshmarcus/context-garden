import math
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from garden.runs import Run, RunStore
from garden.scheduler import Scheduler
from garden.scheduler.snapshot import _safe
from garden.store import Store
from garden.web.app import create_app
from garden.web.common import Hub
from tests.conftest import complete_brief


def client(garden):
    # TestClient addresses the app as http://testserver; bind the origin check there so a
    # browser's own-origin POST (Origin: http://testserver) is accepted.
    return TestClient(create_app(Store(garden), watch=False, host="testserver"))


def test_pages_render(garden):
    c = client(garden)
    for url in ["/", "/board", "/trellis", "/runs", "/phases/demo/p1", "/tasks/DM-001", "/tasks/DM-001/brief", "/partials/board", "/api/tasks", "/events", "/trials", "/costs"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert "DM-002" in c.get("/board").text
    assert "Inbox zero" in c.get("/").text
    assert c.get("/tasks/NOPE").status_code == 404


@pytest.mark.parametrize("history_size", [1546, 6000])
def test_initial_pages_stay_bounded_with_large_run_history(garden, history_size):
    rs = RunStore(garden / ".garden")
    for n in range(history_size):
        run_dir = rs.dir / f"DM-{n % 2 + 1:03d}" / f"20260101T{n:06d}Z-work"
        Run(task_id=f"DM-{n % 2 + 1:03d}", run_id=run_dir.name, dir=str(run_dir), runner="local",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:01:00+00:00",
            status="done", cost_usd=0.01).save()
    for n in range(3):
        run_dir = rs.dir / f"LIVE-{n}" / f"20260906T17000{n}Z-work"
        Run(task_id=f"LIVE-{n}", run_id=run_dir.name, dir=str(run_dir), runner="local", pid=os.getpid(),
            started_at="2026-09-06T17:00:00+00:00", status="running").save()
    c = client(garden)
    timings = []
    scans = rs.scan_count
    reads = rs.read_count
    urls = ("/", "/board", "/partials/board", "/now2", "/now2/period")
    for interval in range(3):
        for url in urls * 4:
            started = time.perf_counter()
            assert c.get(url).status_code == 200
            timings.append(time.perf_counter() - started)
        if interval < 2:
            time.sleep(rs.MAX_INDEX_AGE_SECONDS + 0.05)

    p95 = sorted(timings)[math.ceil(0.95 * len(timings)) - 1]
    print(f"{history_size + 3} runs, 3 active: n={len(timings)} page p95={p95:.3f}s "
          f"max={max(timings):.3f}s scans={rs.scan_count - scans} reads={rs.read_count - reads}")
    assert p95 < 2.0
    assert p95 <= max(timings)
    assert rs.read_count - reads == history_size + 3


def test_page_reader_does_not_run_scheduler_startup_mutations(garden, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("read-only page construction ran a scheduler migration")

    monkeypatch.setattr(Scheduler, "_migrate_fence_bookkeeping", unexpected)
    monkeypatch.setattr(Scheduler, "_hold_startup_config_against_fences", unexpected)
    reader = Hub(Store(garden), watch=False).reader()
    assert reader.control() == {}


def test_pages_refuse_partial_totals_when_archive_index_is_corrupt(garden):
    archive = garden / ".garden" / "run-archive"
    archive.mkdir(parents=True)
    (archive / "index.json").write_text("not json")
    response = client(garden).get("/board")
    assert response.status_code == 503
    assert "history is temporarily unavailable" in response.text.lower()


def test_design_files_are_safe_and_use_the_product_checkout(garden):
    repo = garden.parent / "repo"
    (repo / "docs" / "design").mkdir(parents=True)
    (repo / "docs" / "design" / "mock.html").write_text("<h1>Mock</h1><script>bad()</script>")
    (repo / "docs" / "design" / "notes.md").write_text("# Notes\n\nA design note.")
    (repo / "docs" / "design" / "pixel.png").write_bytes(b"PNG bytes")
    c = client(garden)

    assert "Design" in c.get("/").text
    assert c.get("/design").status_code == 200
    html = c.get("/design/mock.html")
    assert html.status_code == 200 and "<script>bad()</script>" in html.text
    assert html.headers["content-security-policy"] == "sandbox"
    markdown = c.get("/design/notes.md")
    assert markdown.status_code == 200 and "<h1>Notes</h1>" in markdown.text
    image = c.get("/design/pixel.png")
    assert image.status_code == 200 and image.content == b"PNG bytes"
    assert c.get("/design/%2e%2e/README.md").status_code == 404
    assert c.get("/design/%2Fetc%2Fpasswd").status_code == 404


def test_snapshot_scrubs_sensitive_strings_not_just_field_names():
    value = _safe({"message": "failed in /home/alice/repo with token=abc123 and ghp_secret",
                   "error": "Authorization: Bearer xyz"})
    text = str(value)
    assert "/home/alice/repo" not in text
    assert "abc123" not in text and "ghp_secret" not in text and "xyz" not in text


def test_run_page_links_and_serves_every_capture_type(garden):
    run_dir = garden / ".garden" / "runs" / "DM-001" / "capture-run"
    run = Run(task_id="DM-001", run_id="capture-run", dir=str(run_dir), runner="local",
              started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:01:00+00:00",
              status="done", result={"captures": [str(run_dir / "ui" / "page.png"),
                                                  str(run_dir / "ui" / "page.html"),
                                                  "notes.md", "run.json"]})
    run.save()
    (run_dir / "ui").mkdir()
    (run_dir / "ui" / "page.png").write_bytes(b"png")
    (run_dir / "ui" / "page.html").write_text("<script>bad()</script>")
    (run_dir / "notes.md").write_text("notes")
    c = client(garden)
    page = c.get("/runs/DM-001/capture-run")
    assert page.status_code == 200
    assert "page.png" in page.text and "page.html" in page.text and "notes.md" in page.text
    assert "/ui/{" not in page.text
    for name, content_type in (("ui/page.png", "image/png"), ("ui/page.html", "text/html"), ("notes.md", "text/markdown")):
        response = c.get(f"/runs/DM-001/capture-run/captures/{name}")
        assert response.status_code == 200 and response.content
        assert response.headers["content-type"].startswith(content_type)
    assert c.get("/runs/DM-001/capture-run/captures/../run.json").status_code == 404
    html = c.get("/runs/DM-001/capture-run/captures/ui/page.html")
    assert html.headers["content-security-policy"] == "sandbox"
    assert c.get("/runs/DM-001/capture-run/captures/run.json").status_code == 404
    (run_dir / "garden.yaml").write_text("token: secret")
    assert c.get("/runs/DM-001/capture-run/captures/garden.yaml").status_code == 404


def test_task_page_shows_required_evidence_states(garden):
    from garden.scheduler import State

    store = Store(garden)
    task = store.task("DM-001")
    task.extra["requires"] = ["persona-review -p designer", "captures", "check: unit"]
    store.save(task)
    state = State(store.config.garden_dir / "state.json")
    state.get(task.id)["required_evidence"] = {
        "persona:designer": "running", "capture:": "posted", "check:unit": "queued",
    }
    state.save()
    page = client(garden).get("/tasks/DM-001").text
    assert "Required evidence" in page
    assert "persona review · designer" in page and "UI captures" in page and "check · unit" in page
    assert "running" in page and "posted" in page and "queued" in page


def test_header_has_seedling_mark_and_favicon(garden):
    c = client(garden)
    page = c.get("/").text
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in page
    assert '<a class="wordmark" href="/">' in page
    assert 'class="mark"' in page and "st-sprout" in page
    fav = c.get("/favicon.svg")
    assert fav.status_code == 200
    assert fav.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in fav.text and "viewBox=\"0 0 24 24\"" in fav.text


def test_board_columns_and_list_views(garden):
    c = client(garden)
    # Default is the columns view.
    cols = c.get("/board")
    assert cols.status_code == 200
    assert 'class="board"' in cols.text
    assert "viewswitch" in cols.text and "columns" in cols.text and "list" in cols.text
    # The list view groups tasks by status with section headings and per-state facts.
    lst = c.get("/board?view=list")
    assert lst.status_code == 200
    assert 'class="board-list"' in lst.text
    assert 'class="lgroup' in lst.text
    assert "DM-001" in lst.text and "DM-002" in lst.text
    # A blocked task shows what it waits on in the list.
    assert "waits on" in lst.text
    # Both views are reachable through the live-refresh partial.
    assert 'class="board"' in c.get("/partials/board?view=columns").text
    assert 'class="board-list"' in c.get("/partials/board?view=list").text
    # The switch and filters carry the chosen view so navigation keeps it.
    assert "view=list" in lst.text


def test_board_list_surfaces_a_waiting_question(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    c = client(garden)
    text = c.get("/partials/board?view=list").text
    assert "waiting_human".replace("_", " ") in text
    assert "Q: Postgres" in text


def test_actions(garden):
    c = client(garden)
    r = c.post("/tasks/DM-002/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert "cancelled" in c.get("/tasks/DM-002").text
    # CG-142: cancelled is terminal, so a stale retry click is refused, not reopened
    r = c.post("/tasks/DM-002/retry", follow_redirects=False)
    assert r.status_code == 303
    assert "DM-002 is cancelled" in c.get(r.headers["location"]).text
    assert "cancelled" in c.get("/tasks/DM-002").text
    c.post("/tasks/DM-001/unapprove")
    assert "draft" in c.get("/api/tasks").json()[0]["status"]
    complete_brief(garden, "DM-001")  # CG-193: approve refuses a task with no real criteria
    c.post("/phases/demo/p1/approve-all")
    assert c.get("/api/tasks").json()[0]["status"] == "ready"


def test_review_done_escape_hatch_is_confirmed_and_not_primary(garden):
    from garden.model import Status
    from garden.store import Store

    task = Store(garden).task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://github.com/test/demo/pull/71"
    Store(garden).save(task)
    c = client(garden)
    task_page = c.get("/tasks/DM-001").text
    inbox = c.get("/").text

    assert "Mark done without merging" in task_page
    assert "Mark this task done without merging its PR?" in task_page
    assert 'action="/tasks/DM-001/done"' in inbox
    assert "Mark done without merging" in inbox
    assert "Mark this task done without merging its PR?" in inbox
    primary_actions, escape_hatch = inbox.split('class="escape-hatch"')
    assert 'action="/tasks/DM-001/done"' not in primary_actions
    assert 'action="/tasks/DM-001/done"' in escape_hatch


def test_task_page_lists_stashed_changes(garden):
    """CG-198: the named stash a dispatch set aside on a dirty worktree is listed on the task
    page so a person can recover it."""
    from garden.scheduler import State

    state = State(Store(garden).config.garden_dir / "state.json")
    state.get("DM-001")["stashes"] = [{"name": "garden:DM-001:2026-09-05T13:00:00+00:00",
                                       "sha": "aa0ade13b0536e80a91c3852bae858cc84cc9163",
                                       "at": "2026-09-05T13:00:00+00:00"}]
    state.save()
    page = client(garden).get("/tasks/DM-001").text
    assert "Stashed changes" in page
    assert "aa0ade13b053" in page and "git stash apply" in page


def test_trial_with_one_contender_shows_a_message_not_a_500(garden):
    c = client(garden)
    r = c.post("/tasks/DM-001/trial", data={"note": "claude:sonnet"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "a trial needs at least two contenders, e.g. claude:sonnet, claude:opus" in page


def test_trial_form_picks_contenders_from_config(garden):
    """CG-087: the trial form seeds harness/model selects from garden.yaml's harnesses (here
    claude and codex, see the `garden` fixture) instead of a free-text harness:model field."""
    from garden.scheduler import Scheduler
    from garden.store import Store

    c = client(garden)
    page = c.get("/tasks/DM-001").text
    assert "data-trial-form" in page and "trial-rows" in page and "+ Add contender" in page
    assert '"claude"' in page and '"codex"' in page  # harness_choices embedded for the JS selects to read
    assert '"sonnet"' in page and '"gpt-std"' in page  # each harness's tier map

    r = c.post("/tasks/DM-001/trial", data={"note": "claude:sonnet, claude:sonnet"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "must be distinct" in page

    r = c.post("/tasks/DM-001/trial", data={"note": "claude:sonnet, claude:opus"}, follow_redirects=False)
    assert r.status_code == 303
    assert "flash" not in r.headers["location"]
    sched = Scheduler(Store(garden))
    trial = sched.state.get("DM-001").get("trial")
    assert trial and {c["label"] for c in trial["contenders"]} == {"claude:sonnet", "claude:opus"}


def test_review_action_bypasses_the_cap_when_one_was_reached(garden):
    """The 'One more automated review' button on a review-capped task and the plain
    'Automated review' button both post to /tasks/{id}/review; either way the web action
    must go through Scheduler.review_again (not dispatch_review directly), or the cap-bypass
    button silently fails to raise the cap or clear the needs_human stop."""
    from garden.model import Status
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/1"
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["review_rounds"] = 2  # == the default review.max_rounds; the cap has been reached
    st["needs_human"] = {"kind": "review_cap", "reason": "2 automated review round(s) used"}
    sched.state.save()

    c = client(garden)
    r = c.post("/tasks/DM-001/review", follow_redirects=False)
    assert r.status_code == 303

    sched2 = Scheduler(Store(garden))
    st = sched2.state.get("DM-001")
    assert not st.get("needs_human")  # the stop is cleared, not left dangling
    assert st["review_rounds"] == 2  # rolled back one by the bypass, then re-incremented on dispatch
    assert st.get("review_run")


def test_task_actions_refuse_a_merged_done_task(garden, monkeypatch):
    """CG-142: automerge marks a task `done`; a stale page's triage-ready/review click that
    lands afterward must be refused, named with the state and reason, not silently reopen the
    task or dispatch a review for a PR that already merged."""
    from garden.model import Status
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    task.status = Status.AWAITING_TRIAGE
    sched.store.save(task)
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    sched._transition(task, Status.DONE, f"PR merged: {task.pr}")

    c = client(garden)
    for action in ("triage-ready", "review", "retry", "dispatch"):
        r = c.post(f"/tasks/DM-001/{action}", follow_redirects=False)
        assert r.status_code == 303, action
        page = c.get(r.headers["location"]).text
        assert "DM-001 is done: #71 was merged" in page, (action, page)

    assert Store(garden).task("DM-001").status == Status.DONE  # never moved back into the loop
    assert not sched.runs.runs_for("DM-001")  # no review or work run was dispatched


def test_done_task_with_stale_state_shows_no_needs_you_badge_on_board(garden):
    """CG-195: a done task carrying a stale needs_human flag (from before a terminal
    transition cleared it, or a hand-edited state.json) must not wear a 'needs you' badge in
    the done column."""
    from garden.model import Status
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    sched.state.save()

    c = client(garden)
    page = c.get("/board").text
    assert "badge hot" not in page
    page = c.get("/board?view=list").text
    assert "badge hot" not in page


def test_merged_task_page_with_stale_state_says_nothing_about_automerge(garden):
    """CG-195: a merged task's page must never say automerge is held, even when the state
    still carries a stale automerge_blocked (e.g. set the same tick the merge happened)."""
    from garden.model import Status
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    task.status = Status.DONE
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["automerge_blocked"] = "the automated review verdict is request_changes, not approve"
    sched.state.save()

    c = client(garden)
    page = c.get("/tasks/DM-001").text
    assert "held:" not in page


def test_scheduler_errors_flash_a_message_instead_of_500(garden):
    """CG-092: a task whose precondition changed underneath the person (here: DM-001 is
    'ready', not 'waiting_human') must say so on the page, not 500 or silently drop the
    submitted text."""
    c = client(garden)
    r = c.post("/tasks/DM-001/answer", data={"note": "SQLite, please"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "no longer waiting for you" in page
    assert "SQLite, please" in page  # the typed answer is preserved, not lost

    r = c.post("/tasks/DM-001/reject", data={"note": "no"}, follow_redirects=False)
    assert r.status_code == 303
    assert "has no pending worker decision to reject" in c.get(r.headers["location"]).text


def test_a_page_render_exception_flashes_instead_of_500(garden, monkeypatch, caplog):
    """CG-185: a GET whose page handler raises while building the page (here, forced by a
    broken markdown renderer — a stand-in for any template/render-time failure, the same
    failure mode as the tojson-on-Undefined incident) must show the person a page with the
    header and navigation still up and a flash explaining something went wrong, not a bare 500
    with a traceback body — and the traceback must reach the log with the request path."""
    import logging

    from garden.web.pages import task as task_page

    def boom(text):
        raise RuntimeError("boom: broken markdown render")

    monkeypatch.setattr(task_page, "render_md", boom)
    # ServerErrorMiddleware always re-raises after handing our handler's response to the
    # client (so a real server still logs it); TestClient must not re-raise it into the test.
    c = TestClient(create_app(Store(garden), watch=False), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="garden.web"):
        r = c.get("/tasks/DM-001")
    assert r.status_code == 500
    assert "Something went wrong rendering this page" in r.text
    assert "Inbox" in r.text and "wordmark" in r.text  # the shell (nav/header) still renders
    assert "Traceback" not in r.text and "RuntimeError" not in r.text  # no raw traceback to the person
    assert any("/tasks/DM-001" in rec.message and rec.exc_info for rec in caplog.records)


def test_unexpected_action_exception_shows_a_generic_message(garden, monkeypatch):
    from garden.scheduler import Scheduler

    def boom(self, task, note="cancelled"):
        raise ValueError("boom")

    monkeypatch.setattr(Scheduler, "cancel", boom)
    c = client(garden)
    r = c.post("/tasks/DM-001/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert "something failed; see the log" in c.get(r.headers["location"]).text


def test_phase_and_global_actions_flash_a_message_instead_of_500(garden, monkeypatch):
    """CG-122: extend the flash pattern from task_action to friction-report and the
    phase/global actions (approve-all, persona, plan, pause, resume, upgrade)."""
    from garden.scheduler import Scheduler

    def boom(self, *a, **k):
        raise RuntimeError("boom")

    c = client(garden)

    monkeypatch.setattr(Scheduler, "pause", boom)
    r = c.post("/pause", follow_redirects=False)
    assert r.status_code == 303
    assert "boom" in c.get(r.headers["location"]).text

    monkeypatch.setattr(Scheduler, "resume", boom)
    r = c.post("/resume", follow_redirects=False)
    assert r.status_code == 303
    assert "boom" in c.get(r.headers["location"]).text

    monkeypatch.setattr(Scheduler, "upgrade", boom)
    r = c.post("/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert "boom" in c.get(r.headers["location"]).text

    monkeypatch.setattr(Scheduler, "dispatch_persona_phase", boom)
    r = c.post("/phases/demo/p1/persona", data={"personas": "security"}, follow_redirects=False)
    assert r.status_code == 303
    assert "boom" in c.get(r.headers["location"]).text


def test_persona_and_friction_report_404_on_unknown_phase(garden):
    c = client(garden)
    assert c.post("/phases/demo/nope/persona", data={"personas": "security"}).status_code == 404
    assert c.post("/phases/demo/nope/plan").status_code == 404
    assert c.post("/friction-report", data={"product": "demo", "phase": "nope", "text": "slow"}).status_code == 404


def test_approve_all_flashes_a_message_instead_of_500(garden, monkeypatch):
    from garden.store import Store

    c = client(garden)
    c.post("/tasks/DM-001/unapprove")  # DM-001 is now draft, so approve-all has work to do
    complete_brief(garden, "DM-001")  # CG-193: get past the brief gate so save() is what fails

    def boom(self, task):
        raise RuntimeError("save boom")

    monkeypatch.setattr(Store, "save", boom)
    r = c.post("/phases/demo/p1/approve-all", follow_redirects=False)
    assert r.status_code == 303
    assert "save boom" in c.get(r.headers["location"]).text


def test_friction_report_files_and_redirects(garden):
    c = client(garden)
    r = c.post("/friction-report", data={"product": "demo", "phase": "p1", "text": "the brief was confusing"}, follow_redirects=False)
    assert r.status_code == 303


def test_events_page_and_answer_flow(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    c = client(garden)
    page = c.get("/tasks/DM-001").text
    assert "waiting for you" in page and "Postgres or SQLite?" in page
    assert c.get("/events").status_code == 200 and "waiting_human" in c.get("/events").text
    assert "Q: Postgres" in c.get("/partials/board").text
    r = c.post("/tasks/DM-001/answer", data={"note": "SQLite"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/api/tasks").json()[0]["status"] == "running"
    page = c.get("/tasks/DM-001").text
    assert "Questions and answers" in page and "SQLite" in page and "Timeline" in page


def test_task_page_shares_pending_worker_decision_card_with_inbox(garden):
    """A notification can safely link here even if the report outlives its transition."""
    from garden.model import Status
    from garden.scheduler import State
    from garden.store import Store

    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.AWAITING_TRIAGE
    store.save(task)
    state = State(store.config.garden_dir / "state.json")
    state.get(task.id)["decision"] = {
        "kind": "no_change", "reason": "The code is already correct.",
        "final": "I checked the requested path and found no change to make.",
    }
    state.save()

    c = client(garden)
    for page in (c.get("/").text, c.get(f"/tasks/{task.id}").text):
        assert 'class="panel decision-card"' in page
        assert "The code is already correct." in page
        assert "The worker's full message" in page
        assert f'action="/tasks/{task.id}/accept"' in page
        assert f'action="/tasks/{task.id}/reject"' in page


def test_task_page_shares_waiting_question_card_and_omits_it_without_a_decision(garden, monkeypatch):
    from garden.model import Status
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    c = client(garden)
    page = c.get("/tasks/DM-001").text
    assert 'class="panel decision-card"' in page
    assert "Postgres or SQLite?" in page
    assert 'action="/tasks/DM-001/answer"' in page

    state = sched.state
    task = sched.store.task("DM-001")
    task.status = Status.READY
    sched.store.save(task)
    state.get(task.id).pop("question", None)
    state.save()
    assert 'class="panel decision-card"' not in c.get("/tasks/DM-001").text


def test_inbox_and_task_page_include_the_same_decision_card_fragment():
    from pathlib import Path

    templates = Path(__file__).parents[1] / "src" / "garden" / "web" / "templates"
    for name in ("inbox.html", "task.html"):
        assert '{% include "_decision_card.html" %}' in (templates / name).read_text()


def test_inbox_attention_cards_keep_their_discuss_prompts_separate(garden):
    """Each shared card targets its own discuss prompt when the Inbox has several stops."""
    from garden.model import Status
    from garden.store import Store

    store = Store(garden)
    for task_id in ("DM-001", "DM-002"):
        task = store.task(task_id)
        task.status = Status.FAILED
        store.save(task)

    page = client(garden).get("/").text
    for task_id in ("DM-001", "DM-002"):
        assert f'data-toggle="discuss-{task_id}"' in page
        assert f'id="discuss-{task_id}"' in page
        assert f'id="discuss-text-{task_id}"' in page
        assert f'data-copy="discuss-text-{task_id}"' in page
    assert 'id="discuss-panel"' not in page
    assert 'id="discuss-text"' not in page


def test_budget_form_and_route(garden):
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    c = client(garden)
    page = c.get("/phases/demo/p1").text
    assert "/phases/demo/p1/budget" in page and "no cap" in page
    # it applies on blur, a single field, no Set button beside it
    assert 'data-autosave' in page and 'onblur="this.form.requestSubmit()"' in page
    assert "<button>Set</button>" not in page
    # Set a cap.
    r = c.post("/phases/demo/p1/budget", data={"amount": "42"}, follow_redirects=False)
    assert r.status_code == 303
    assert Scheduler(Store(garden), github=FakeGitHub()).budget_for("demo/p1") == 42.0
    assert "of $42" in c.get("/phases/demo/p1").text
    assert "set at runtime" in c.get("/config").text
    # Clearing the field (an empty amount) switches it back off; `no_budget` also still
    # works directly against the route for anything posting to it besides the page's form.
    r = c.post("/phases/demo/p1/budget", data={"amount": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert Scheduler(Store(garden), github=FakeGitHub()).budget_for("demo/p1") == 0.0
    r = c.post("/phases/demo/p1/budget", data={"amount": "42"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/phases/demo/p1/budget", data={"no_budget": "1", "amount": "42"}, follow_redirects=False)
    assert r.status_code == 303
    assert Scheduler(Store(garden), github=FakeGitHub()).budget_for("demo/p1") == 0.0
    # A non-numeric amount is rejected.
    assert c.post("/phases/demo/p1/budget", data={"amount": "abc"}).status_code == 400


def test_new_task_form_renders_on_phase_page(garden):
    c = client(garden)
    page = c.get("/phases/demo/p1").text
    assert 'id="new-task"' in page
    assert 'action="/phases/demo/p1/new-task"' in page
    for field in ("title", "goal", "context", "acceptance", "difficulty", "priority", "reading", "depends_on", "ready"):
        assert f'name="{field}"' in page
    assert "+ new task" in page  # the rail link (CG-132)


def test_new_task_matches_cli_new_task_for_the_same_inputs(garden, monkeypatch):
    """The web form must produce the same file `garden new-task` would for the same
    title/deps/reading/priority/difficulty, when the free-text body fields are left blank."""
    from garden.store import Store
    from tests.test_cli import run

    # Store.create_task stamps created/updated via garden.store's now_iso, but Store.save
    # calls Task.touch(), which stamps updated via garden.model's own now_iso binding; both
    # must be frozen or the two calls' timestamps can straddle a second on a loaded CI box.
    monkeypatch.setattr("garden.store.now_iso", lambda: "2026-02-02T00:00:00+00:00")
    monkeypatch.setattr("garden.model.now_iso", lambda: "2026-02-02T00:00:00+00:00")

    r = run(garden, "new-task", "demo/p1", "Third: thing", "--dep", "DM-001", "--read", "demo/p1/specs/spec.md")
    assert r.exit_code == 0 and "DM-003" in r.output
    store = Store(garden)
    expected = store.task("DM-003").path.read_text()
    store.task("DM-003").path.unlink()
    store.invalidate()

    c = client(garden)
    r = c.post("/phases/demo/p1/new-task", data={
        "title": "Third: thing", "depends_on": "DM-001", "reading": "demo/p1/specs/spec.md",
        "difficulty": "medium", "priority": "3",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/tasks/DM-003")

    store.invalidate()
    assert store.task("DM-003").path.read_text() == expected


def test_new_task_fills_in_the_body_from_the_form(garden):
    c = client(garden)
    r = c.post("/phases/demo/p1/new-task", data={
        "title": "Write the docs", "goal": "Explain the thing.", "context": "Nobody knows how it works.",
        "acceptance": "- [ ] docs exist\n- [ ] \n- [ ] reviewed", "difficulty": "easy", "priority": "1",
        "reading": "demo/p1/specs/spec.md",
        "ready": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/tasks/DM-003")
    task_page = c.get(r.headers["location"]).text
    assert "Explain the thing." in task_page
    assert "Nobody knows how it works." in task_page
    assert "docs exist" in task_page and "reviewed" in task_page
    assert "created DM-003" in task_page  # flash message (CG-086)

    from garden.store import Store
    t = Store(garden).task("DM-003")
    assert t.status.value == "ready"  # approve now was ticked
    assert t.difficulty == "easy"
    assert t.priority == 1


def test_new_task_validation_keeps_typed_text_and_flashes_a_message(garden):
    c = client(garden)
    r = c.post("/phases/demo/p1/new-task", data={
        "title": "", "goal": "keep me", "depends_on": "NOPE", "difficulty": "medium", "priority": "3",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "new-task" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "a title is required" in page
    assert "unknown task" in page and "NOPE" in page
    assert "keep me" in page  # typed text survives the round trip


def test_new_task_rejects_an_unresolved_reading_path(garden):
    c = client(garden)
    r = c.post("/phases/demo/p1/new-task", data={
        "title": "Some task", "reading": "demo/p1/specs/nope.md", "difficulty": "medium", "priority": "3",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "does not exist" in page and "nope.md" in page


def test_new_task_approve_now_refusal_keeps_it_draft_and_flashes_the_gap(garden):
    """CG-238: the new-task form's approve-now checkbox goes through Scheduler.approve, the
    same gate a hand approval uses. Left blank, the acceptance-criteria field seeds the
    template's placeholder ("- [ ] ..."), so brief_gaps refuses it -- the task is still
    created, but stays a draft, and the flash names the gap and the task's file."""
    c = client(garden)
    r = c.post("/phases/demo/p1/new-task", data={
        "title": "Untested idea", "difficulty": "medium", "priority": "3", "ready": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/tasks/DM-003")
    page = c.get(r.headers["location"]).text
    assert "placeholder" in page
    assert "demo/p1/tasks/DM-003" in page

    from garden.model import Status
    from garden.store import Store

    assert Store(garden).task("DM-003").status == Status.DRAFT


def test_inline_edit_clears_brief_gate(garden):
    """A draft with a missing checklist can repair its brief on its task page and approve."""
    from garden.model import Status

    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.DRAFT
    store.save(task)
    c = client(garden)

    page = c.get("/tasks/DM-001").text
    assert 'id="brief-card"' in page
    assert 'action="/tasks/DM-001/brief"' in page

    saved = c.post("/tasks/DM-001/brief", data={
        "acceptance": "- [ ] The task page saves a repaired brief, covered by this test.",
        "reading": "demo/p1/specs/spec.md",
    })
    assert saved.status_code == 200 and saved.json() == {"gaps": []}
    task = Store(garden).task("DM-001")
    assert "## Acceptance criteria\n\n- [ ] The task page saves a repaired brief" in task.body
    assert task.reading == ["demo/p1/specs/spec.md"]
    assert 'id="brief-card"' not in c.get("/tasks/DM-001").text

    c.post("/tasks/DM-001/approve")
    assert Store(garden).task("DM-001").status == Status.READY


def test_inline_brief_edit_rejects_missing_reading_path(garden):
    from garden.model import Status

    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.DRAFT
    store.save(task)

    response = client(garden).post("/tasks/DM-001/brief", data={
        "acceptance": "- [ ] The saved brief is valid.",
        "reading": "demo/p1/specs/missing.md",
    })
    assert response.status_code == 422
    assert "reading-list path not found" in response.json()["detail"]
    assert Store(garden).task("DM-001").reading == ["demo/p1/specs/spec.md"]


def test_dispatch_button_is_not_offered_on_a_draft_task(garden):
    """CG-238: dispatching a draft used to skip the approve gate entirely (a placeholder
    brief could be dispatched silently); the only way from draft into the loop now is
    Approve, so the button is not offered at all."""
    from tests.test_cli import run

    assert run(garden, "new-task", "demo/p1", "Idea").exit_code == 0  # DM-003, draft
    c = client(garden)
    page = c.get("/tasks/DM-003").text
    assert 'action="/tasks/DM-003/dispatch"' not in page
    assert 'action="/tasks/DM-003/approve"' in page


def test_dispatch_action_refuses_a_run_in_flight_and_dispatches_cleanly_otherwise(garden):
    """CG-238: a second dispatch press while a run is already in flight for the task must
    not orphan the first one. A clean dispatch redirects without a flash (a flash means a
    refusal throughout the app, including to `garden qa`'s scripted client) -- the new run's
    id is already on the task page, in its Log line and its "Latest run" link."""
    from garden.runner.manual import ManualRunner
    from garden.runs import RunStore
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    sched.dispatch(store.task("DM-001"), runner=ManualRunner({}), worktree=False)

    c = client(garden)
    r = c.post("/tasks/DM-001/dispatch", follow_redirects=True)
    assert "already has a run in flight" in r.text
    assert Store(garden).task("DM-001").status.value == "running"

    r2 = c.post("/tasks/DM-002/dispatch", follow_redirects=False)
    assert r2.status_code == 303 and "flash=" not in r2.headers["location"]
    latest = RunStore(garden / ".garden").latest("DM-002")
    page = c.get(r2.headers["location"]).text
    assert latest.run_id in page  # Log line and "Latest run" link both name it


def test_phase_page_shows_retro_waiting_for_personas(garden):
    from garden.scheduler import Scheduler
    from garden.store import Store

    sched = Scheduler(Store(garden))
    ph = sched.store.phase("demo", "p1")
    entry = {"phase": ph.key, "product": ph.product, "phase_name": ph.name,
             "personas": ["designer", "security", "user"], "skip_personas": False,
             "next_phase": "p2", "self_product": "demo", "stage": "personas", "persona_runs": {}}
    sched._retro_list().append(entry)
    sched.state.save()

    c = client(garden)
    assert "retro: waiting for personas (0 of 3)" in c.get("/phases/demo/p1").text


def test_inbox_and_trials_render_with_no_products_or_trials(tmp_path):
    """CG-185: the strict template environment means a page that forgets a context value a
    `tojson` site (or anything else) needs raises instead of silently defaulting — so these
    two edge-case renders (an inbox on a garden with no products at all, and the trials
    leaderboard with nothing recorded) are the cheapest proof that every page still supplies
    what its templates need on the empty path, not only the happy one with fixture data."""
    from garden.store import Store

    c = TestClient(create_app(Store(tmp_path), watch=False))
    home = c.get("/")
    assert home.status_code == 200 and "Inbox zero" in home.text
    trials = c.get("/trials")
    assert trials.status_code == 200 and "No trials yet" in trials.text


def test_trial_env_failed_contender_renders_on_task_and_trials_pages(sched, fake_github, monkeypatch):
    """CG-229: an env_failed contender and an inconclusive trial carry fields (kind, note,
    kept) that a `pr`/`done` contender never sets; under the strict template environment a
    missing key raises rather than defaulting, so this is the cheapest proof both pages
    still render once a real environment failure has happened, not just the happy path."""
    sched.cfg.data["worker_env"]["pass"].append("FAKE_CODEX_*")
    monkeypatch.setenv("FAKE_CODEX_MODE", "sandboxed")
    t = sched.store.task("DM-001")
    sched.start_trial(t, ["claude:sonnet", "codex:gpt"])
    sched.tick()

    c = TestClient(create_app(sched.store, watch=False))
    task_page = c.get("/tasks/DM-001")
    assert task_page.status_code == 200
    assert "kept claude:sonnet" in task_page.text
    assert "env_failed (sandbox)" in task_page.text
    trials_page = c.get("/trials")
    assert trials_page.status_code == 200 and "env failed" in trials_page.text


def test_task_page_renders_mid_trial_and_after_trial_again(sched, fake_github):
    """CG-317: an active or reset trial has no verdict and may have partial contenders."""
    from garden.model import Status

    task = sched.store.task("DM-001")
    sched.start_trial(task, ["claude:sonnet", "claude:opus"])
    state = sched.state.get("DM-001")
    trial = state["trial"]
    trial.pop("winner", None)
    trial["contenders"][0].pop("score", None)
    trial["contenders"][0].pop("pr", None)
    trial["contenders"][0].pop("cost", None)
    trial["contenders"][0]["status"] = "running"
    trial["contenders"][1].pop("score", None)
    trial["contenders"][1].pop("pr", None)
    trial["contenders"][1].pop("cost", None)
    trial["contenders"][1]["status"] = "failed"
    sched.state.save()

    c = TestClient(create_app(sched.store, watch=False))
    page = c.get("/tasks/DM-001")
    assert page.status_code == 200
    assert "no verdict yet" in page.text
    assert "running" in page.text and "failed" in page.text and "elapsed" in page.text

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://github.com/test/demo/pull/1"
    sched.store.save(task)
    trial["status"] = "done"
    trial["winner"] = "claude:sonnet"
    sched.state.save()
    sched.start_trial(task, ["claude:sonnet", "claude:opus"], again=True)

    page = c.get("/tasks/DM-001")
    assert page.status_code == 200
    assert "no verdict yet" in page.text


def test_task_page_shows_trial_history_with_the_closed_pr_marked(sched, fake_github, monkeypatch):
    """CG-232: once a trial concludes, its record stays visible on the task page (beside a
    later --again's) with the losing contender's now-closed PR marked, not shown as if still
    open."""
    monkeypatch.setenv("FAKE_CLAUDE_WINNER", "claude:opus")
    t = sched.store.task("DM-001")
    sched.start_trial(t, ["claude:sonnet", "claude:opus"])
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")

    monkeypatch.delenv("FAKE_CLAUDE_WINNER", raising=False)
    sched.start_trial(t, ["claude:sonnet", "codex:gpt"], again=True)
    sched.tick()
    sched.tick()

    c = TestClient(create_app(sched.store, watch=False))
    page = c.get("/tasks/DM-001").text
    assert page.count("Model trial for DM-001") == 2  # both the first trial and the --again rerun
    assert "(closed)" in page  # the losing contender's PR, not shown as if still open


def test_trials_page_and_persona_form(garden):
    c = client(garden)
    r = c.get("/trials")
    assert r.status_code == 200 and "No trials yet" in r.text
    assert "Persona review of the body of work" in c.get("/phases/demo/p1").text
    assert c.get("/trellis").status_code == 200 and c.get("/graph").status_code == 200


def test_phase_review_panel_shows_score_and_feature_count(garden):
    """CG-188: the phase page's review list shows a persona's score, and for a persona whose
    report has a `features` section, the count of features."""
    reviews = garden / "demo" / "p1" / "docs" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "product-manager-2026-09-05.md").write_text(
        "# product-manager review of demo/p1\n\n**Persona:** product-manager · **Score:** 8/10 · 2026-09-05\n\n"
        "Solid.\n\n## Features\n\n- **A form to file a task**\n  - lets a user file without markdown\n"
        "- **Cost per phase on the phase page**\n  - a manager sees spend\n\n## Medium\n\n- **onboarding** — needs a config file\n")
    text = client(garden).get("/phases/demo/p1").text
    assert "persona · product-manager · 8/10 · 2 feature(s)" in text


def test_retro_page_renders_the_artefacts(garden):
    """CG-146: once a phase's retro has run, its page shows the reconciled document (with the
    friction verdicts), the operator retro, each persona's report with its score and high
    findings, and the tasks the retro filed — even ones filed into a later phase."""
    docs = garden / "demo" / "p1" / "docs"
    (docs / "retro").mkdir(parents=True)
    (docs / "retro.md").write_text(
        "# Retrospective: demo/p1\n\n## What changed\n\nHalved the hand actions.\n\n"
        "## Friction reconciled\n\n| Friction item | Verdict |\n|---|---|\n"
        "| worktree has no venv | fixed |\n")
    (docs / "retro" / "operator.md").write_text(
        "# Operator retro\n\nThe loop ran for an hour, not a night.\n")
    (docs / "reviews").mkdir()
    (docs / "reviews" / "designer-2026-09-05.md").write_text(
        "# Persona review: designer\n\n**Score:** 7/10 · 2026-09-05\n\nCoherent overall.\n\n"
        "## High\n\n- names differ across surfaces\n")
    p2 = garden / "demo" / "p2" / "tasks"
    p2.mkdir(parents=True)
    (p2 / "DM-050-followup.md").write_text(
        "---\nid: DM-050\ntitle: Retro follow-up\nstatus: draft\nproduct: demo\nphase: p2\n"
        "discovered_from: retro:demo/p1\ncreated: '2026-09-05T00:00:00+00:00'\n"
        "updated: '2026-09-05T00:00:00+00:00'\n---\n\n## Goal\n\nSomething the retro found.\n")

    c = client(garden)
    r = c.get("/phases/demo/p1/retro")
    assert r.status_code == 200
    text = r.text
    assert "Halved the hand actions." in text  # reconciled document
    assert "worktree has no venv" in text and "fixed" in text  # friction table with verdicts
    assert "The loop ran for an hour" in text  # operator retro
    assert "designer" in text and "7/10" in text  # persona table with its score
    assert "names differ across surfaces" in text  # its high findings
    assert "DM-050" in text and "Retro follow-up" in text  # tasks from this retro, across phases
    # the phase page links to it
    assert "/phases/demo/p1/retro" in c.get("/phases/demo/p1").text


def test_retro_page_says_no_retro_yet(garden):
    c = client(garden)
    r = c.get("/phases/demo/p1/retro")
    assert r.status_code == 200 and "no retro yet" in r.text
    # and the phase page shows no retro link until the retro has run
    assert "/phases/demo/p1/retro" not in c.get("/phases/demo/p1").text


def test_trellis_and_phase_hide_done_toggle(garden):
    c = client(garden)
    c.post("/tasks/DM-002/cancel", follow_redirects=False)

    full = c.get("/trellis")
    assert 'href="/tasks/DM-002"' in full.text
    assert "hide done (1)" in full.text

    hidden = c.get("/trellis?hide=done")
    assert hidden.status_code == 200
    assert 'href="/tasks/DM-002"' not in hidden.text
    assert "show 1 done" in hidden.text

    full_phase = c.get("/phases/demo/p1")
    assert "DM-002" in full_phase.text and "hide 1 done" in full_phase.text

    hidden_phase = c.get("/phases/demo/p1?hide=done")
    assert "DM-002" not in hidden_phase.text
    assert "show 1 done" in hidden_phase.text


def test_inbox_triage_flow(garden, monkeypatch):
    import yaml

    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["github"] = {"draft_pr": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    gh = FakeGitHub()
    sched = Scheduler(store, github=gh)
    sched.tick()
    sched.tick()
    c = TestClient(create_app(store, watch=False, host="testserver"))
    home = c.get("/").text
    assert "Triage a draft PR" in home and "DM-001" in home and "Ready for review" in home
    r = c.post("/tasks/DM-001/triage-changes", data={"note": "tighten the tests"}, headers={"referer": "http://testserver/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/")
    assert next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"] == "changes_requested"
    sched.tick()
    sched.tick()
    assert "awaiting_triage" in next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"]
    c.post("/tasks/DM-001/triage-ready", follow_redirects=False)
    assert next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"] == "in_review"
    assert "Review and merge" in c.get("/").text


def test_inbox_shows_a_paused_harness_notice(garden):
    from garden.scheduler import Scheduler

    sched = Scheduler(Store(garden))
    sched.pause_harness("claude", "quota limit hit on claude")
    c = client(garden)
    home = c.get("/").text
    assert "Harness paused" in home and "claude" in home and "quota limit hit on claude" in home


def test_failed_worker_decision_card_keeps_evidence_and_actions_separate(garden):
    """A long run id must not squeeze the decision text under an action column."""
    from garden.model import Status
    from garden.runs import RunStore

    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.FAILED
    task.pr = "https://github.com/test/demo/pull/312"
    task.body += "\n## Log\n\n- the worker failed after a long reason about the configuration reload\n"
    store.save(task)
    run = RunStore(store.config.garden_dir).new_run(
        task.id, "local", run_id="20260906T010102Z-revise-with-an-unusually-long-suffix"
    )
    run.status = "failed"
    run.error = "The configuration change could not be trusted until the active worker finishes."
    run.save()

    inbox = client(garden).get("/").text
    card = inbox[inbox.index('<div class="item'):inbox.index("</section>", inbox.index('<div class="item'))]
    assert run.run_id in card and "Continue the loop" in card and "Open PR" in card
    assert card.index('class="what decision-content"') < card.index('class="decision-evidence"')
    assert card.index('class="decision-evidence"') < card.index('class="card-actions decision-actions"')
    assert 'class="decision-action"' in card

    task_page = client(garden).get("/tasks/DM-001").text
    assert 'class="panel decision-card"' in task_page
    assert 'class="decision-evidence"' in task_page
    assert 'class="decision-actions"' in task_page

    base = (Path(__file__).parents[1] / "src/garden/web/templates/base.html").read_text()
    decision_layout = base[base.index(".decision-card"):base.index("/* ---- trellis")]
    assert "position:absolute" not in decision_layout
    # The 18rem text track leaves enough measure for words at both the 1280px
    # desktop layout and the 390px phone layout. These widths account for the
    # shell, page/card padding, glyph column, and grid gap.
    assert "grid-template-columns:34px minmax(18rem,1fr)" in base
    assert "grid-template-columns:26px minmax(18rem,1fr)" in base
    desktop_text_width = 1280 - 236 - (2 * 32) - 2 - (2 * 12) - 34 - 14
    phone_text_width = 390 - (2 * 14) - 2 - (2 * 12) - 26 - 10
    assert desktop_text_width >= 18 * 16
    assert phone_text_width >= 18 * 16
    assert "@media (max-width:600px)" in base


def test_stdout_partial(garden):
    c = client(garden)
    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "no output yet" in r.text

    # Write JSONL events into a fake run dir and verify the partial reflects them
    import json

    from garden.runs import RunStore
    from garden.store import Store
    store = Store(garden)
    rs = RunStore(store.config.garden_dir)
    run = rs.new_run("DM-001", "local", "work")
    (run.path / "stdout.json").write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}) + "\n" +
        json.dumps({"type": "result", "subtype": "success", "result": "Done."}) + "\n"
    )
    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "Bash" in r.text and "ls" in r.text


def test_stdout_partial_handles_string_and_list_tool_result_content(garden):
    """A stream-json run mixes tool_result.content shapes (string and list of blocks); the
    task page must render both instead of 500ing (CG-104)."""
    import yaml

    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["harnesses"]["claude"]["output_format"] = "stream-json"
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    sched.tick()

    c = client(garden)
    r = c.get("/tasks/DM-001")
    assert r.status_code == 200
    assert "abc1234 fake change" in r.text  # string tool_result.content
    assert "working" in r.text  # list tool_result.content, first text block

    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "abc1234 fake change" in r.text and "working" in r.text


def _record_run(garden, *, status="done", harness="claude", stdout="", brief="", final="", stderr=""):
    """Write a run directory on disk with the given recorded files and return the Run."""
    from garden.runs import RunStore
    from garden.store import Store

    rs = RunStore(Store(garden).config.garden_dir)
    run = rs.new_run("DM-001", "local", "work")
    run.status = status
    run.harness = harness
    run.model = "sonnet"
    run.save()
    if stdout:
        (run.path / "stdout.json").write_text(stdout)
    if brief:
        (run.path / "brief.md").write_text(brief)
    if final:
        (run.path / "final.md").write_text(final)
    if stderr:
        (run.path / "stderr.log").write_text(stderr)
    return run


def test_run_page_stream_json(garden):
    """A stream-json run page renders the transcript (assistant text, tool calls with their
    command, tool results and the result), plus brief, final message and stderr tabs. The
    task page lists the run and links to its page."""
    import json

    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on the task"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "3 passed"}]}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "All finished."}),
    ]) + "\n"
    run = _record_run(garden, stdout=stdout, brief="# The brief\n\nDo the first thing.",
                      final="All finished.\nGARDEN_RESULT: {\"status\": \"done\"}", stderr="a warning line")

    c = client(garden)
    task_page = c.get("/tasks/DM-001").text
    assert f"/runs/DM-001/{run.run_id}" in task_page  # the Runs section links to the run page

    body = c.get(f"/runs/DM-001/{run.run_id}").text
    assert "Working on the task" in body                 # assistant text
    assert "Bash" in body and "pytest -q" in body        # tool call with its command
    assert "3 passed" in body                            # tool result
    assert "The brief" in body                           # brief tab
    assert "All finished." in body                       # final message tab
    assert "a warning line" in body                      # stderr tab
    assert "/partials/runs/DM-001/" not in body          # a finished run does not tail


def test_run_page_claude_json_shows_final_text(garden):
    """A claude-json run is a single result object with no transcript; the page falls back to
    the final text."""
    import json

    stdout = json.dumps({"type": "result", "subtype": "success",
                         "result": "Implemented the thing.\nGARDEN_RESULT: {\"status\": \"done\"}",
                         "usage": {"input_tokens": 10}, "total_cost_usd": 0.01}) + "\n"
    run = _record_run(garden, stdout=stdout)  # no brief/final/stderr files on disk

    body = client(garden).get(f"/runs/DM-001/{run.run_id}").text
    assert "Implemented the thing." in body   # final text, recovered from the result object
    assert "claude-json" in body              # the fallback note names the format
    assert "no brief recorded" in body        # missing files render gracefully
    assert "stderr was empty" in body


def test_run_page_running_tails_the_same_view(garden):
    """A running run opens the same page and keeps tailing via the poll hook; a 404 for an
    unknown run."""
    import json

    stdout = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "starting"}]}}) + "\n"
    run = _record_run(garden, status="running", stdout=stdout)

    c = client(garden)
    body = c.get(f"/runs/DM-001/{run.run_id}").text
    assert "starting" in body
    assert f"data-poll=\"/partials/runs/DM-001/{run.run_id}/stdout\"" in body
    assert c.get(f"/partials/runs/DM-001/{run.run_id}/stdout").status_code == 200
    assert c.get("/runs/DM-001/nope").status_code == 404


def test_timeline_formats_the_new_event_kinds(garden):
    """The Timeline gives a phrase to the states phase-03 added: mechanical and agent rebases,
    the merge-queue head and its drops, ignored feedback, a failed retro step, a stale-base
    recovery and a phase freeze. Task-less events (a retro, a freeze) link the phase."""
    from garden.events import EventLog
    from garden.store import Store

    log = EventLog(Store(garden).config.garden_dir / "events.jsonl")
    log.emit("rebase", "DM-001", base="main", files=[], resolved=True, how="mechanical", run="r1")
    log.emit("rebase", "DM-001", base="main", files=["a.py"], resolved=False, how="agent")
    log.emit("merge_head", "DM-001", waiting=True, reason="rebased; awaiting rollup")
    log.emit("merge_head", "DM-002", left=True, reason="checks failed")
    log.emit("feedback_ignored", "DM-001", author="stranger", reason="untrusted")
    log.emit("retro_failed", "", phase="demo/p1", step="persona", error="the reviewer crashed")
    log.emit("rebased_stale_base", "DM-001", base="main", base_sha="abc123def456", resolved=True)
    log.emit("phase_frozen", "", phase="demo/p1", frozen=True)

    text = client(garden).get("/events").text
    assert "rebased onto main mechanically" in text
    assert "conflict on main in a.py" in text
    assert "merge queue head" in text
    assert "left the merge queue: checks failed" in text
    assert "ignored feedback from stranger (untrusted)" in text
    assert "demo/p1 retro: persona failed" in text
    assert "moved and recovered" in text
    assert "demo/p1 frozen" in text
    assert '/phases/demo/p1' in text  # the task-less events link the phase, not an empty task

    # The per-task page timeline labels the same task-carrying kinds (not blank fallbacks).
    task_page = client(garden).get("/tasks/DM-001").text
    assert "rebased onto main mechanically" in task_page
    assert "merge queue head" in task_page
    assert "ignored feedback from stranger" in task_page


def test_run_page_mechanical_rebase(garden):
    """A mechanical rebase run has no harness and no transcript: its page says what it is,
    shows what git did, and shows the pre-PR check run that followed it."""
    from garden.runs import RunStore
    from garden.store import Store

    rs = RunStore(Store(garden).config.garden_dir)
    rebase = rs.new_run("DM-001", "local", "rebase")
    rebase.status, rebase.base, rebase.cost_usd = "done", "main", 0.0
    rebase.diff_stat = " src/app.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)"
    rebase.save()
    check = rs.new_run("DM-001", "local", "check")
    check.status = "done"
    check.result = {"checks": [{"name": "unit", "status": "ok", "summary": "3 passed"}]}
    check.save()

    body = client(garden).get(f"/runs/DM-001/{rebase.run_id}").text
    assert "Mechanical rebase onto" in body and "main" in body
    assert "no model, no cost" in body
    assert "1 file changed" in body            # what git did
    assert "unit" in body and "3 passed" in body  # the follow-on check result
    assert f"/runs/DM-001/{check.run_id}" in body  # links to the check run
    assert 'data-tab="transcript"' not in body     # no transcript tabs for a git-only run


def test_inbox_shows_the_merge_queue(garden):
    from garden.events import EventLog
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub

    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    assert sched.store.task("DM-001").status.value == "in_review"
    st = sched.state.get("DM-001")
    st["merge_head"] = True
    st["automerge_candidate"] = True
    st["checks"] = "PENDING"
    sched.state.save()
    EventLog(Store(garden).config.garden_dir / "events.jsonl").emit(
        "merge_head", "DM-002", left=True, reason="a human requested changes")

    page = client(garden).get("/").text
    assert "Merge queue" in page
    assert "DM-001" in page and "waiting on CI" in page
    assert "Last drop" in page and "a human requested changes" in page


def test_drawings_render_unescaped(garden, tmp_path):
    """Plant and stage drawings are inline SVG, not escaped text (a Jinja autoescape regression)."""
    c = TestClient(create_app(Store(garden), watch=False, plates_dir=tmp_path / "plates"))
    for url in ["/", "/board", "/phases/demo/p1", "/tasks/DM-001", "/trellis"]:
        html = c.get(url).text
        assert "&lt;svg" not in html, url
        assert '<use href="#pea"/>' in html, url  # the rail shows every phase's plant
        if url != "/":  # the fixture inbox is empty, so it shows no stage glyphs
            assert '<use href="#st-' in html, url
    phase = c.get("/phases/demo/p1").text
    assert '<use href="#pea"/>' in phase
    assert "Plate I" in phase
    assert phase.count('class="bg-vine"') == 1  # the background vine, once per page


def test_scanned_plates_replace_the_drawing_when_present(garden, tmp_path):
    plates = tmp_path / "plates"
    c = TestClient(create_app(Store(garden), watch=False, plates_dir=plates))
    html = c.get("/phases/demo/p1").text
    assert '<use href="#pea"/>' in html and 'class="plate"' not in html  # nothing fetched yet: the drawing
    (plates / "pea.webp").write_bytes(b"RIFF....WEBP")
    html = c.get("/phases/demo/p1").text
    assert '<img class="plate" src="/static/plates/pea.webp"' in html
    assert "plate: Thomé, Flora von Deutschland, 1885" in html
    assert c.get("/static/plates/pea.webp").status_code == 200
    assert c.get("/static/plates/bramble.webp").status_code == 404
    # the rail thumbnail uses the thumb file, or the plate itself until a thumb exists
    assert 'src="/static/plates/pea.webp" alt="" width="38"' in html
    (plates / "pea-thumb.webp").write_bytes(b"RIFF....WEBP")
    assert 'src="/static/plates/pea-thumb.webp"' in c.get("/").text


def test_specimen_label_names_the_plates_own_species_when_it_differs(garden, tmp_path):
    # The bramble plant is the R. fruticosus aggregate, but its actual plate is Thomé's Tafel
    # 398, Rubus thyrsoideus — the label names the plate's own species alongside the plant's.
    (garden / "demo" / "p2").mkdir()
    (garden / "demo" / "p2" / "goals.md").write_text("---\nplant: bramble\nplate: II\n---\n# p2\n\nGoals.\n")
    (garden / "demo" / "p2" / "tasks").mkdir()
    plates = tmp_path / "plates"
    c = TestClient(create_app(Store(garden), watch=False, plates_dir=plates))
    (plates / "bramble.webp").write_bytes(b"RIFF....WEBP")
    html = c.get("/phases/demo/p2").text
    assert "plate: Thomé, Flora von Deutschland, 1885, Tafel 398, Rubus thyrsoideus" in html
    # a plant whose plate matches its own species names nothing extra
    (plates / "pea.webp").write_bytes(b"RIFF....WEBP")
    html = c.get("/phases/demo/p1").text
    assert html.count("plate: Thomé, Flora von Deutschland, 1885") == 1
    assert "Tafel" not in html


def test_friction_report_web(garden):
    c = client(garden)
    r = c.post(
        "/friction-report",
        data={"product": "demo", "phase": "p1", "text": "The form is confusing.", "page": "/inbox"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "## Reported" in text
    assert "The form is confusing." in text
    # A draft task was created
    from garden.store import Store
    tasks = Store(garden).tasks()
    friction_tasks = [t for t in tasks.values() if "confusing" in t.title]
    assert friction_tasks, "expected a draft task for the friction report"
    assert friction_tasks[0].status.value == "draft"


def test_friction_report_web_with_task_id(garden):
    c = client(garden)
    r = c.post(
        "/friction-report",
        data={"product": "demo", "phase": "p1", "text": "Brief is too long.", "page": "/tasks/DM-001", "task_id": "DM-001"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    text = doc.read_text()
    assert "DM-001" in text
    assert "Brief is too long." in text


def test_inbox_page_head_subtitle_is_not_capped_narrow(garden):
    """CG-184: `.page-head p` used to cap at 62ch, wrapping the Inbox subtitle onto two
    lines on a wide screen. The left column now grows to fill the space instead."""
    html = client(garden).get("/").text
    assert "62ch" not in html
    assert "Every item here is a decision only a person can make." in html


def test_friction_form_in_inbox_and_task(garden):
    c = client(garden)
    assert "Report friction" in c.get("/").text
    assert "Report friction" in c.get("/tasks/DM-001").text


def test_friction_form_phase_select_is_scoped_per_product(garden):
    """CG-157: the inbox friction form used two independent selects, so a phase belonging to
    one product could be submitted alongside another product and 404. The phase select is now
    built client-side from a per-product map, so it can only ever offer phases of the chosen
    product."""
    import json
    import re

    from tests.conftest import write

    write(garden / "acme" / "product.md", "# acme\n\nAnother product.\n")
    write(garden / "acme" / "q1" / "goals.md", "# q1\n\nShip it.\n")

    html = client(garden).get("/").text
    assert '<select name="phase" id="friction-phase" style="margin-bottom:6px"></select>' in html
    m = re.search(r"data-phases='([^']*)'", html)
    assert m, "expected the product select to carry a data-phases map"
    assert json.loads(m.group(1)) == {"demo": ["p1"], "acme": ["q1"]}


def test_config_page_renders(garden):
    c = client(garden)
    r = c.get("/config")
    assert r.status_code == 200
    assert "Pause" in r.text
    assert "max_parallel" in r.text
    assert "auto_dispatch" in r.text
    assert "Config" in r.text


def test_task_page_names_the_review_ladder_rung(garden):
    """A ladder-routed review makes its writer/reviewer relationship visible on the task."""
    from garden.runs import RunStore

    run = RunStore(Store(garden).config.garden_dir).new_run("DM-001", "local", mode="review")
    run.harness = "codex"
    run.model = "gpt-5.6-sol"
    run.env_snapshot = {"writer_model": "gpt-5.6-terra"}
    run.save()

    assert "reviewed by gpt-5.6-sol, one above gpt-5.6-terra" in client(garden).get("/tasks/DM-001").text


def test_config_page_names_live_and_restart_keys(garden):
    """CG-192: the page says config is re-read each tick without a restart, and names the
    keys that still need one (RESTART_KEYS)."""
    c = client(garden)
    text = c.get("/config").text
    assert "within one tick" in text and "no restart" in text
    assert "Needs a restart" in text
    assert "work_dir" in text and "tick_interval" in text
    live_values = text.split("Live values", 1)[1].split("Read once at startup", 1)[0]
    assert "tick_interval" not in live_values


def test_config_page_and_inbox_show_a_held_reload_and_accept_applies_it(garden, monkeypatch):
    """CG-242: garden.yaml changes while a dispatched run is still in flight — driven entirely
    through `/tick`, the same path `garden serve`'s own watch loop uses. The Config page and
    Inbox must show the hold, and confirming it from the Config page applies it on the next
    tick even though the run has not been reaped."""
    import yaml

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")  # DM-001's run never finishes on its own
    c = client(garden)
    assert c.post("/tick", follow_redirects=False).status_code == 303  # dispatch DM-001

    data = yaml.safe_load((garden / "garden.yaml").read_text())
    data["notify"] = {"command": "true"}
    (garden / "garden.yaml").write_text(yaml.safe_dump(data))

    assert c.post("/tick", follow_redirects=False).status_code == 303  # held: DM-001 is still in flight and unreaped
    config_page = c.get("/config").text
    assert "Held config reload" in config_page and "notify.command" in config_page
    assert "Confirm a held config change" in c.get("/").text

    r = c.post("/config/accept-reload", follow_redirects=False)
    assert r.status_code == 303

    assert c.post("/tick", follow_redirects=False).status_code == 303  # applies now, even though DM-001 is still running
    assert "Held config reload" not in c.get("/config").text
    assert "Confirm a held config change" not in c.get("/").text


def test_accept_reload_with_nothing_held_flashes_a_message(garden):
    c = client(garden)
    r = c.post("/config/accept-reload", follow_redirects=False)
    assert r.status_code == 303
    assert "no config reload is held" in c.get(r.headers["location"]).text


def test_pause_resume_web(garden):
    c = client(garden)
    # not paused by default
    assert "dispatch paused" not in c.get("/").text
    # pause via web
    r = c.post("/pause", data={"reason": "testing"}, follow_redirects=False)
    assert r.status_code == 303
    home = c.get("/").text
    assert "dispatch paused" in home
    config_page = c.get("/config").text
    assert "testing" in config_page
    assert "Resume dispatch" in config_page
    # resume via web
    r = c.post("/resume", follow_redirects=False)
    assert r.status_code == 303
    home = c.get("/").text
    assert "dispatch paused" not in home
    config_page = c.get("/config").text
    assert "Pause dispatch" in config_page


def test_max_parallel_override_from_config_page(garden):
    c = client(garden)
    config_page = c.get("/config").text
    assert "garden.yaml: <strong>2</strong>" in config_page
    assert "no live override" in config_page
    # the field applies on blur/Enter; no Set button beside it
    assert 'data-autosave' in config_page and 'onblur="this.form.requestSubmit()"' in config_page
    assert "<button class=\"primary\">Set</button>" not in config_page
    assert "0/2" in c.get("/").text  # inbox header: workers running / live limit

    r = c.post("/config/max-parallel", data={"value": "5"}, follow_redirects=False)
    assert r.status_code == 303
    config_page = c.get("/config").text
    assert "live override: <strong>5</strong>" in config_page
    assert "0/5" in c.get("/").text

    # an empty value clears the override — the same endpoint, no separate Clear button
    r = c.post("/config/max-parallel", data={"value": ""}, follow_redirects=False)
    assert r.status_code == 303
    config_page = c.get("/config").text
    assert "no live override" in config_page
    assert "0/2" in c.get("/").text

    assert c.post("/config/max-parallel", data={"value": "nope"}).status_code == 400
    assert c.post("/config/max-parallel", data={"value": "0"}).status_code == 400


def test_observe_profile_override_from_config_page(garden):
    """CG-219: the Config page can switch `garden observe`'s profile live, the same way it
    overrides max_parallel — a running `--follow` reads the override on its next pass."""
    from garden.observe import resolve
    from garden.scheduler import Scheduler
    from garden.store import Store

    c = client(garden)
    config_page = c.get("/config").text
    assert "no live override" in config_page
    for name in ("quiet", "watch", "debug"):
        assert f'value="{name}"' in config_page and f'>{name}</option>' in config_page

    r = c.post("/config/observe-profile", data={"value": "watch"}, follow_redirects=False)
    assert r.status_code == 303
    config_page = c.get("/config").text
    assert "live override: <strong>watch</strong>" in config_page

    sched = Scheduler(Store(garden), log=print)
    assert resolve(sched.cfg, sched).profile == "watch"

    r = c.post("/config/observe-profile", data={"value": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "no live override" in c.get("/config").text


def test_operating_profile_switch_from_the_rail_and_config_page(garden):
    """CG-221: the rail slider and the Config page both post to the same live override, no
    Set button, a plain select and form post — and the switch is visible everywhere within
    a tick: dispatch's worker count, the review tier and the observe feed."""
    from garden.observe import resolve
    from garden.scheduler import Scheduler
    from garden.store import Store

    c = client(garden)
    home = c.get("/").text
    assert "Operating profile" in home
    assert "plain garden.yaml values" in home
    for name in ("economy", "balanced", "fast"):
        assert f'value="{name}"' in home and f'>{name}</option>' in home
    assert "<button>Set</button>" not in home and ">Set<" not in home

    config_page = c.get("/config").text
    assert "no live override" in config_page
    assert "economy" in config_page and "balanced" in config_page and "fast" in config_page

    r = c.post("/config/operating-profile", data={"value": "fast"}, follow_redirects=False)
    assert r.status_code == 303
    config_page = c.get("/config").text
    assert "live override: <strong>fast</strong>" in config_page
    home = c.get("/").text
    assert 'value="fast" selected' in home or 'selected>fast<' in home

    # the Parallelism and observe-profile panels say the *stop*, not garden.yaml, answers
    # max_parallel and observe.profile now — the "which values come from the stop" criterion
    assert "no live override" in config_page  # neither knob has its own direct override
    assert "from the operating profile <span class=\"mono\">fast</span>" in config_page

    sched = Scheduler(Store(garden), log=print)
    from garden.profiles import BUILTIN_PROFILES

    assert sched.effective_max_parallel() == BUILTIN_PROFILES["fast"]["workers"]
    assert sched.effective("review.difficulty") == BUILTIN_PROFILES["fast"]["review_difficulty"]
    assert resolve(sched.cfg, sched).profile == BUILTIN_PROFILES["fast"]["observe"]

    r = c.post("/config/operating-profile", data={"value": "nonexistent"}, follow_redirects=False)
    assert r.status_code == 303  # flashed error, not a 500
    assert sched.operating_profile_name() == "fast"  # unchanged

    r = c.post("/config/operating-profile", data={"value": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "no live override" in c.get("/config").text


def test_operating_profile_spend_rate_shown_on_the_rail(garden):
    """The rail's money line only appears once there is something to show — an idle garden
    stays quiet (CG-205's "no fake zero" rule for cost display applies here too)."""
    from garden.runs import RunStore
    from garden.store import Store

    c = client(garden)
    assert "/hr last hour" not in c.get("/").text

    rs = RunStore(Store(garden).config.garden_dir)
    run = rs.new_run("DM-001", "local", mode="work")
    run.finished_at = run.started_at
    run.cost_usd = 1.23
    run.save()
    assert "$1.23/hr last hour" in c.get("/").text


def test_priority_and_difficulty_from_the_task_page(garden):
    from garden.model import PRIORITY_SCALE
    from garden.store import Store

    c = client(garden)
    r = c.post("/tasks/DM-001/difficulty", data={"note": "hard"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/tasks/DM-001/priority", data={"note": "0"}, follow_redirects=False)
    assert r.status_code == 303
    t = Store(garden).task("DM-001")
    assert t.difficulty == "hard" and t.priority == 0
    assert "difficulty medium -> hard (web)" in t.body and "priority" in t.body
    assert c.post("/tasks/DM-001/difficulty", data={"note": "extreme"}, follow_redirects=False).status_code == 400
    page = c.get("/tasks/DM-001").text
    assert 'name="note"' in page and 'value="hard" selected' in page
    # both selects apply on change, no Set button beside either
    assert 'onchange="this.form.requestSubmit()"' in page
    assert "<button class=\"quiet\">Set</button>" not in page
    # priority options are words with the number beside them, ordered first to last
    for word, n in PRIORITY_SCALE:
        assert f">{word} · {n}<" in page
    assert page.index("first · 0") < page.index("next · 1") < page.index("normal · 2") < page.index("later · 3") < page.index("someday · 4")
    # posting each scale value stores the number and renders it selected
    for _word, n in PRIORITY_SCALE:
        r = c.post("/tasks/DM-001/priority", data={"note": str(n)}, follow_redirects=False)
        assert r.status_code == 303
        t = Store(garden).task("DM-001")
        assert t.priority == n
        page = c.get("/tasks/DM-001").text
        assert f'value="{n}" selected' in page
    # a priority outside the scale shows as its number and stays selectable
    c.post("/tasks/DM-001/priority", data={"note": "9"}, follow_redirects=False)
    t = Store(garden).task("DM-001")
    assert t.priority == 9
    page = c.get("/tasks/DM-001").text
    assert 'value="9" selected' in page


def test_no_set_apply_save_buttons_in_any_template():
    """CG-190: editing an existing value applies on change; only forms that create
    something new (a task, a friction report, a persona run, ...) keep a submit button."""
    import re

    from garden.web.common import TEMPLATES

    button_re = re.compile(r"<button[^>]*>\s*(Set|Apply|Save)\s*<", re.IGNORECASE)
    offenders = []
    for path in TEMPLATES.glob("*.html"):
        for m in button_re.finditer(path.read_text()):
            offenders.append(f"{path.name}: {m.group(0)!r}")
    assert not offenders, offenders


def test_editable_values_apply_on_change_with_a_saved_mark(garden):
    """Every data-autosave form (the walkthrough's Config and task pages among them) carries
    an autosave-mark slot for the JS-driven saved/undo behaviour."""
    import re

    autosave_form_re = re.compile(r"<form\b[^>]*\bdata-autosave\b")
    for url in ("/config", "/tasks/DM-001", "/phases/demo/p1"):
        page = client(garden).get(url).text
        forms = autosave_form_re.findall(page)
        assert len(forms) >= 1, url
        assert len(forms) == page.count('class="autosave-mark"')


# ---- trust at the edges (CG-154): sanitised HTML, an origin check on POSTs ---------------


def test_rendered_markdown_is_sanitised():
    from garden.web.common import render_md
    from garden.web.trust import safe_json, sanitize_html

    html = render_md(
        "# Title\n\nSome **bold** and a [link](https://example.com/a?b=1&c=2).\n\n"
        "<script>alert(1)</script>\n\n<a href=\"javascript:alert(1)\" onclick=\"x()\">click</a>\n\n"
        "<img src=x onerror=alert(1)>\n\n```py\nif a < b: pass\n```\n\n| a | b |\n|---|---|\n| 1 | <i>2</i> |\n"
    )
    assert "<script" not in html and "alert(1)" not in html
    assert "onclick" not in html and "onerror" not in html and "javascript:" not in html
    assert "<h1>Title</h1>" in html and "<strong>bold</strong>" in html
    assert '<a href="https://example.com/a?b=1&amp;c=2">link</a>' in html
    assert '<code class="language-py">if a &lt; b: pass' in html
    assert "<table>" in html and "<i>2</i>" in html
    assert sanitize_html("<style>x</style><iframe src=//e></iframe>after <b>b</b><u>u</u>") == "after <b>b</b><u>u</u>"
    assert sanitize_html("<a href='data:text/html,x'>d</a><a href='/tasks/X'>r</a>") == '<a>d</a><a href="/tasks/X">r</a>'
    assert safe_json({"k": "</script><'&"}) == '{"k": "\\u003c/script\\u003e\\u003c\\u0027\\u0026"}'


def test_pages_neutralise_agent_written_html(garden):
    """A task body (planner or worker output), pending PR feedback (a commenter) and a spec
    render as prose, never as script or event handlers."""
    from garden.scheduler import State
    from garden.store import Store

    s = Store(garden)
    t = s.task("DM-001")
    t.body += "\n\n<script>alert('body')</script>\n\n<p onmouseover=\"steal()\">hover</p> **fine**\n"
    s.save(t)
    st = State(garden / ".garden" / "state.json")
    st.get("DM-001")["pending_feedback"] = "- **mallory**: <img src=x onerror=\"alert('fb')\"> please <em>rename</em>"
    st.save()
    (garden / "demo" / "p1" / "specs" / "spec.md").write_text("# spec\n\n<iframe src=\"//evil.example\"></iframe>\n\nDetails.\n")
    c = client(garden)
    for url in ("/tasks/DM-001", "/phases/demo/p1"):
        page = c.get(url).text
        assert "alert(" not in page and "onmouseover" not in page and "onerror" not in page and "<iframe" not in page, url
    page = c.get("/tasks/DM-001").text
    assert "<strong>fine</strong>" in page and "hover" in page and "<em>rename</em>" in page


def test_task_page_renders_review_fixes_and_improvements(garden):
    from garden.scheduler import State

    state = State(garden / ".garden" / "state.json")
    state.get("DM-001")["last_review"] = {
        "verdict": "request_changes", "summary": "needs a boundary test",
        "findings": [{"severity": "blocking", "summary": "empty input breaks", "file": "a.py", "line": 2,
                      "fix": "Return early when the input is empty."}],
        "improvements": [{"area": "naming", "suggestion": "Rename x to parsed_value.",
                          "why": "Callers read more clearly.", "effort": "small"}],
    }
    state.save()

    page = client(garden).get("/tasks/DM-001").text
    assert "Return early when the input is empty." in page
    assert "Improvements" in page and "Rename x to parsed_value." in page


def test_posts_from_another_origin_are_refused(garden):
    c = client(garden)
    # A form posted by a page on another site carries its Origin: refused, nothing changes.
    r = c.post("/tasks/DM-002/cancel", headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and "not this server" in r.text
    assert "cancelled" not in c.get("/api/tasks").json()[1]["status"]
    r = c.post("/tick", headers={"Origin": "null"}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post("/pause", headers={"Referer": "http://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    # GETs are never blocked, whatever their Origin.
    assert c.get("/board", headers={"Origin": "http://evil.example"}).status_code == 200
    # The server's own pages post with its Origin (or Referer); a script with neither is not a browser.
    assert c.post("/tasks/DM-001/unapprove", headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303
    assert c.post("/tasks/DM-001/approve", headers={"Referer": "http://testserver/tasks/DM-001"}, follow_redirects=False).status_code == 303
    assert c.post("/tasks/DM-002/cancel", follow_redirects=False).status_code == 303
    assert c.get("/api/tasks").json()[1]["status"] == "cancelled"


def test_trusted_origins_from_config_are_accepted(garden):
    import yaml

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["web"] = {"trusted_origins": ["https://garden.internal/"]}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    c = client(garden)
    assert c.post("/tick", headers={"Origin": "https://garden.internal"}, follow_redirects=False).status_code == 303
    assert c.post("/tick", headers={"Origin": "https://other.internal"}, follow_redirects=False).status_code == 403


def test_origin_check_resists_dns_rebinding(garden):
    """The allowlist is the bound address, not the request's Host: a page whose name was
    rebound to the loopback address carries its own Origin, which is not a bound one, so its
    POST is refused even though Host and Origin agree."""
    c = TestClient(create_app(Store(garden), watch=False, host="127.0.0.1", port=8765))
    # The server's own origin (the address it binds to) is accepted.
    assert c.post("/tick", headers={"Origin": "http://127.0.0.1:8765"}, follow_redirects=False).status_code == 303
    assert c.post("/tick", headers={"Origin": "http://localhost:8765"}, follow_redirects=False).status_code == 303
    # A rebound page: it addresses the server as evil.example (Host) and posts with that Origin.
    r = c.post("/tick", headers={"Host": "evil.example", "Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and "not this server" in r.text
    # The right host on the wrong port is a different origin, and is refused.
    assert c.post("/tick", headers={"Origin": "http://127.0.0.1:9999"}, follow_redirects=False).status_code == 403


def test_action_and_get_stay_fast_while_a_tick_runs_a_slow_check(garden):
    """CG-182: a button press and a page render never wait for a scheduler pass. With a slow
    pre-PR check running (as a run record; here in-process, holding hub.lock during the check
    subprocess), POST /tasks/<id>/<action> returns well under a second and GET / under half a
    second, because actions take a short action-only lock (never the tick's) and GET reads
    directly."""
    import threading
    import time

    from tests.conftest import FakeGitHub

    store = Store(garden)
    # A pre-PR check that takes three seconds; the tick starts it as a check run and the
    # in-process runner runs the `sleep` subprocess (which releases the GIL) inside the pass.
    store.config.data["checks"] = {"pre_pr": [{"name": "slow", "command": "sleep 3"}], "ci": []}
    app = create_app(store, watch=False, github=FakeGitHub())
    c = TestClient(app)
    hub = app.state.hub
    hub.tick()  # dispatch DM-001's worker (finishes in-process)

    done = threading.Event()
    threading.Thread(target=lambda: (hub.tick(), done.set()), daemon=True).start()
    time.sleep(0.6)  # let the pass reap the worker and reach the slow check
    assert not done.is_set(), "the background tick should still be running the slow check"

    t0 = time.monotonic()
    r = c.post("/tasks/DM-001/priority", data={"note": "3"}, follow_redirects=False)
    post_s = time.monotonic() - t0
    t1 = time.monotonic()
    g = c.get("/")
    get_s = time.monotonic() - t1

    assert r.status_code == 303 and g.status_code == 200
    assert not done.is_set(), "the tick was still running while both requests were served"
    assert post_s < 1.0, f"POST waited {post_s:.2f}s for the tick"
    assert get_s < 0.5, f"GET waited {get_s:.2f}s for the tick"
    done.wait(timeout=10)


def test_inbox_renders_taskless_question_once(garden, monkeypatch):
    from garden.scheduler import Scheduler

    question = "Which independent project should we onboard?"
    monkeypatch.setattr(Scheduler, "pending_decisions", lambda self: [
        {"id": "question-test", "kind": "question", "question": question,
         "phase": "demo/p1", "source": "kickoff:demo/p1"}
    ])
    html = client(garden).get("/inbox").text
    assert html.count(question) == 1
    assert "Questions to answer" in html


def test_phase_kickoff_follows_tasks_after_approval(garden):
    html = client(garden).get("/phases/demo/p1").text
    assert html.index('id="kickoff"') > html.index('DM-001')
