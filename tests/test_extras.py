"""Trials, persona reviews, and token-free checks."""


from garden.checks import run_check, run_checks, to_feedback
from garden.model import Status
from garden.personas import DEFAULT_PERSONAS, parse_persona, write_default_personas
from garden.trials import TrialLog, parse_compare, parse_contender


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


# ---- checks -------------------------------------------------------------------
def test_run_check_command_and_python(tmp_path, monkeypatch):
    ok = run_check({"name": "true", "command": "echo hi"}, {"branch": "b"}, cwd=tmp_path)
    assert ok["status"] == "pass"
    bad = run_check({"name": "boom", "command": "echo 'FAILED test_x'; echo \"$GARDEN_BRANCH\"; exit 3"}, {"branch": "feat"}, cwd=tmp_path)
    assert bad["status"] == "fail" and "FAILED test_x" in bad["details"] and "feat" in bad["details"]
    js = run_check({"name": "json", "command": "echo '{\"status\": \"flaky\", \"summary\": \"net\"}'"}, {}, cwd=tmp_path)
    assert js["status"] == "flaky" and js["summary"] == "net"
    monkeypatch.setenv("FAKE_CI_MODE", "fail")
    py = run_check({"name": "plugin", "python": "tests.ci_plugin:analyse"}, {"branch": "feat"})
    assert py["status"] == "fail" and "feat" in py["summary"]
    err = run_check({"name": "nope", "python": "tests.ci_plugin:missing"}, {})
    assert err["status"] == "error"
    fb = to_feedback([ok, bad], "pre-PR check")
    assert "boom" in fb and "FAILED test_x" in fb and "true" not in fb
    assert run_checks([], {}) == []


def test_run_check_killed_or_empty_did_not_finish(tmp_path):
    # A check killed by a signal (e.g. torn down with the server) is recorded as
    # "check did not finish", never as a clean failure with no text.
    killed = run_check({"name": "tests", "command": "kill -TERM $$"}, {}, cwd=tmp_path)
    assert killed["status"] == "error" and "check did not finish" in killed["summary"]
    assert "SIGTERM" in killed["summary"]
    # A non-zero exit that produced no output at all is likewise not a silent failure.
    empty = run_check({"name": "tests", "command": "exit 5"}, {}, cwd=tmp_path)
    assert empty["status"] == "error" and "check did not finish" in empty["summary"]
    # to_feedback still yields text (never an empty string) for these.
    assert to_feedback([killed], "pre-PR check").strip()


def test_run_check_signalled_json_output_is_not_a_pass(tmp_path):
    # A check that prints a valid JSON verdict and is then killed did not run to that
    # verdict: the signal is classified before the JSON is trusted, so it is "did not
    # finish", not the pass the JSON claims.
    killed = run_check({"name": "tests", "command": "printf '{\"status\":\"pass\",\"summary\":\"done\"}'; kill -TERM $$"}, {}, cwd=tmp_path)
    assert killed["status"] == "error" and "check did not finish" in killed["summary"]
    assert "SIGTERM" in killed["summary"]


def test_pre_pr_checks_gate_the_pr(sched, fake_github, garden):
    marker = garden / "checked"
    # Passes at the base (no worker-output.txt yet), so the base probe (CG-131) sees a clean
    # base and this stays a branch-owned failure: fails on the first run's "1", passes on "2".
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": f"touch {marker}; if [ -f worker-output.txt ]; then grep -qx 2 worker-output.txt; fi"}], "ci": []}
    # Checks are detached run records (CG-182): the worker's push starts a pre-PR check run,
    # reaped a tick later; a failure probes the base (another check run) before the revise round.
    dispatched: set[str] = set()
    transitions: set[str] = set()
    for _ in range(8):
        rep = sched.tick()
        dispatched |= set(rep.dispatched)
        transitions |= set(rep.transitions)
        if statuses(sched)["DM-001"] == "in_review":
            break
    assert marker.exists()
    assert any(d.startswith("DM-001(check:") for d in dispatched)
    assert "DM-001 -> changes_requested (checks)" in transitions and "DM-001(revise)" in dispatched
    revise = next(r for r in sched.runs.runs_for("DM-001") if r.mode == "revise")
    brief = (revise.path / "brief.md").read_text()
    assert "pre-PR check" in brief and "unit" in brief
    assert statuses(sched)["DM-001"] == "in_review" and len(fake_github.created) == 1


def test_ci_checks_feed_revise_and_flaky_rerun(sched, fake_github, tmp_path, monkeypatch):
    rerun_file = tmp_path / "rerun"
    monkeypatch.setenv("FAKE_CI_RERUN_FILE", str(rerun_file))
    sched.cfg.data["checks"] = {"pre_pr": [], "ci": [{"name": "plugin", "python": "tests.ci_plugin:analyse"}]}
    sched.tick()
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    # 1) flaky -> the CI analyser runs as a detached check run (CG-182); its continuation reruns
    #    CI instead of dispatching a revise round
    monkeypatch.setenv("FAKE_CI_MODE", "flaky")
    pr.updated_at, pr.checks, pr.failed_checks = "t2", "FAILURE", ["build"]
    dispatched = set(sched.tick().dispatched)  # poll starts the CI check run
    rep = sched.tick()  # reap it: flaky -> rerun
    dispatched |= set(rep.dispatched)
    assert rerun_file.read_text().strip() == "rerun"
    assert not any("revise" in d for d in dispatched)
    assert statuses(sched)["DM-001"] == "in_review"
    # 2) real failure -> revise brief carries the analyser's details
    monkeypatch.setenv("FAKE_CI_MODE", "fail")
    pr.updated_at = "t3"
    sched.tick()  # poll starts the CI check run
    rep = sched.tick()  # reap it: real failure -> revise
    assert "DM-001(revise)" in rep.dispatched
    revise = next(r for r in sched.runs.runs_for("DM-001") if r.mode == "revise")
    brief = (revise.path / "brief.md").read_text()
    assert "failed checks: build" in brief and "test_x.py::test_y" in brief


# ---- trials -------------------------------------------------------------------
def test_parse_contender():
    assert parse_contender("claude:opus", "claude") == ("claude:opus", "claude", "opus")
    assert parse_contender("sonnet", "claude") == ("claude:sonnet", "claude", "sonnet")
    assert parse_contender("codex:", "claude") == ("codex", "codex", "")
    assert parse_compare('x\nGARDEN_COMPARE: {"winner": "a", "ranking": [{"label": "a", "score": 9}]}')["winner"] == "a"
    assert parse_compare("nothing") == {}


def test_trial_end_to_end(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WINNER", "claude:opus")
    t = sched.store.task("DM-001")
    runs = sched.start_trial(t, ["claude:sonnet", "claude:opus"])
    assert [r.model for r in runs] == ["sonnet", "opus"]
    assert {r.branch for r in runs} == {"garden/dm-001-first-task-trial-claude-sonnet", "garden/dm-001-first-task-trial-claude-opus"}
    assert statuses(sched)["DM-001"] == "running"
    rep = sched.tick()  # both contenders finished -> two PRs -> comparison run
    assert "DM-001(compare)" in rep.dispatched
    assert len(fake_github.created) == 2 and all(c["title"].startswith("[trial ") for c in fake_github.created)
    trial = sched.state.get("DM-001")["trial"]
    assert trial["status"] == "comparing" and all(c["status"] == "pr" for c in trial["contenders"])
    rep = sched.tick()  # comparison reaped -> winner kept, loser closed
    assert "DM-001 -> in_review (trial winner claude:opus)" in rep.transitions
    t = sched.store.task("DM-001")
    assert t.branch.endswith("-trial-claude-opus") and t.pr.endswith(f"/pull/{fake_github.prs[t.branch].number}")
    loser = fake_github.prs["garden/dm-001-first-task-trial-claude-sonnet"]
    assert loser.number in fake_github.closed and loser.state == "CLOSED"
    assert sum("Model trial" in c for c in fake_github.comments) == 2
    rows = TrialLog(sched.cfg.garden_dir / "trials.jsonl").leaderboard()
    by = {r["label"]: r for r in rows}
    assert by["claude:opus"]["wins"] == 1 and by["claude:opus"]["avg_score"] == 9.0 and by["claude:sonnet"]["avg_score"] == 6.0
    assert by["claude:sonnet"]["avg_cost"] == 0.05
    # the winner's worktree is now the task's worktree, and DM-002 stacks on the winning branch
    assert sched.worktree_for(t).name.endswith("trial-claude-opus")
    assert statuses(sched)["DM-002"] == "running" and sched.runs.latest("DM-002").base == t.branch


def test_trial_single_survivor(sched, fake_github, monkeypatch):
    t = sched.store.task("DM-001")
    sched.cfg.data["harnesses"]["codex"]["bin"] = "/nonexistent/codex"  # this contender crashes
    sched.start_trial(t, ["claude:sonnet", "codex:gpt"])
    rep = sched.tick()
    assert "DM-001 -> in_review (trial winner claude:sonnet)" in rep.transitions
    trial = sched.state.get("DM-001")["trial"]
    assert [c["status"] for c in trial["contenders"]] == ["pr", "failed"]


# ---- personas -----------------------------------------------------------------
def test_default_personas_written(tmp_path):
    out = write_default_personas(tmp_path)
    assert {p.stem for p in out} == set(DEFAULT_PERSONAS)
    assert write_default_personas(tmp_path) == []  # idempotent
    assert parse_persona('GARDEN_PERSONA: {"persona": "user", "score": 5, "overall": "x", "findings": []}')["score"] == 5


def test_persona_phase_review_writes_report_and_tasks(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_PERSONA_SEVERITY", "high")
    sched.tick()
    sched.tick()  # DM-001 has a PR: part of the body of work
    ph = sched.store.phase("demo", "p1")
    run = sched.dispatch_persona_phase(ph, "usability-expert", file_tasks=True)
    brief = (run.path / "brief.md").read_text()
    assert "# Persona: Usability expert" in brief and "Body of work" in brief and "DM-001" in brief and "A fake change" in brief
    rep = sched.tick()
    reports = list((ph.path / "docs" / "reviews").glob("usability-expert-*.md"))
    assert len(reports) == 1 and "First run needs a config file" in reports[0].read_text()
    assert any("persona usability-expert report" in t for t in rep.transitions)
    sched.store.invalidate()
    filed = [t for t in sched.store.tasks().values() if t.discovered_from == "persona:usability-expert"]
    assert len(filed) == 1 and filed[0].status == Status.DRAFT
    # the planner sees the report via docs/
    from garden.planner import plan_prompt

    assert "usability-expert" in plan_prompt(sched.store, "demo", "p1")


def test_persona_pr_review_comments_and_can_request_changes(sched, fake_github, monkeypatch):
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")
    monkeypatch.setenv("FAKE_CLAUDE_PERSONA_SEVERITY", "low")
    sched.dispatch_persona_pr(t, "security")
    rep = sched.tick()
    assert any("security review of DM-001" in c for c in fake_github.comments)
    assert statuses(sched)["DM-001"] == "in_review"
    monkeypatch.setenv("FAKE_CLAUDE_PERSONA_SEVERITY", "high")
    sched.dispatch_persona_pr(sched.store.task("DM-001"), "security", request_changes=True)
    rep = sched.tick()
    assert "DM-001 -> changes_requested (persona security)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    assert "security persona" in (sched.runs.latest("DM-001").path / "brief.md").read_text()


def test_configured_personas_run_on_every_pr(sched, fake_github):
    sched.cfg.data["review"] = {"enabled": False, "personas": ["user"], "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    rep = sched.tick()
    assert "DM-001(persona:user)" in rep.dispatched
    sched.tick()
    assert any("user review of DM-001" in c for c in fake_github.comments)
