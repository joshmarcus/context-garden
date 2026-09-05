from fastapi.testclient import TestClient

from garden.store import Store
from garden.web.app import create_app


def client(garden):
    return TestClient(create_app(Store(garden), watch=False))


def test_pages_render(garden):
    c = client(garden)
    for url in ["/", "/board", "/trellis", "/runs", "/phases/demo/p1", "/tasks/DM-001", "/tasks/DM-001/brief", "/partials/board", "/api/tasks", "/events", "/trials"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert "DM-002" in c.get("/board").text
    assert "Inbox zero" in c.get("/").text
    assert c.get("/tasks/NOPE").status_code == 404


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
    c.post("/phases/demo/p1/approve-all")
    assert c.get("/api/tasks").json()[0]["status"] == "ready"


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


def test_trials_page_and_persona_form(garden):
    c = client(garden)
    r = c.get("/trials")
    assert r.status_code == 200 and "No trials yet" in r.text
    assert "Persona review of the body of work" in c.get("/phases/demo/p1").text
    assert c.get("/trellis").status_code == 200 and c.get("/graph").status_code == 200


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
    c = TestClient(create_app(store, watch=False))
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
