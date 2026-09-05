"""A quota/spend-limit error from a harness pauses dispatch for that harness and returns
the task to ready without burning an attempt (CG-212); a cheap probe resumes it later. A
quota hit mid-revise or mid-rebase instead returns the task to changes_requested with its
feedback (or pending rebase) restored, since that round always has an open PR to protect."""

import pytest

from garden.model import Status

from .conftest import statuses


def test_claude_quota_returns_task_to_ready_without_burning_an_attempt(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()  # dispatch DM-001
    assert sched.store.task("DM-001").attempts == 1
    rep = sched.tick()  # reap: the spend-limit message comes back
    assert "DM-001 -> ready (env_error: quota)" in rep.transitions
    task = sched.store.task("DM-001")
    assert task.attempts == 0  # the attempt dispatch() counted is given back
    assert statuses(sched)["DM-001"] == "ready"
    assert "not counted as an attempt" in task.body


def test_quota_pauses_the_harness_and_notes_it(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()
    assert sched.is_harness_paused("claude")
    entry = sched.paused_harnesses()["claude"]
    assert "quota" in entry["reason"]


def test_paused_harness_blocks_dispatch_until_resumed(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()  # DM-001 back to ready, claude paused
    assert statuses(sched)["DM-001"] == "ready"
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    rep = sched.tick()  # dispatch would normally pick DM-001 up again
    assert rep.dispatched == []
    assert statuses(sched)["DM-001"] == "ready"


def test_probe_resumes_a_recovered_harness(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()
    assert sched.is_harness_paused("claude")
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}  # probe on every tick, for the test
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")  # the account recovered
    rep = sched.tick()
    assert not sched.is_harness_paused("claude")
    assert any("resumed" in t for t in rep.transitions)
    # the harness probe and dispatch are both steps of the same tick, so DM-001 is
    # redispatched in this very pass, not held over to the next one
    assert "DM-001(work)" in rep.dispatched


def test_probe_leaves_the_harness_paused_while_still_over_quota(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    rep = sched.tick()  # probe runs, still quota
    assert sched.is_harness_paused("claude")
    assert rep.dispatched == []  # DM-001 was not dispatched: still paused


def test_probe_does_not_run_before_its_interval(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")  # would resume if probed
    rep = sched.tick()  # default interval (10 minutes) has not elapsed
    assert sched.is_harness_paused("claude")
    assert not any("resumed" in t for t in rep.transitions)


def test_claude_quota_during_revise_round_returns_to_changes_requested_with_feedback(sched, monkeypatch):
    """A quota hit mid-revise must not discard the open PR's pending feedback or misroute
    the task to a fresh work dispatch: it goes back to changes_requested with the same
    feedback queued, and the revision round dispatch() counted is given back."""
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/demo/pull/1"
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix the thing"
    st["revisions"] = 1
    sched.state.save()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()  # dispatch the revise round: clears pending_feedback, bumps revisions
    rep = sched.tick()  # reap: the spend-limit message comes back
    assert "DM-001 -> changes_requested (env_error: quota)" in rep.transitions
    assert statuses(sched)["DM-001"] == "changes_requested"
    st = sched.state.get("DM-001")
    assert st["pending_feedback"] == "- fix the thing"
    assert st["revisions"] == 1
    task = sched.store.task("DM-001")
    assert task.pr == "https://example.com/demo/pull/1"
    assert sched.is_harness_paused("claude")


def test_claude_quota_during_rebase_round_returns_to_changes_requested_with_rebase_pending(sched, monkeypatch):
    """Same for a rebase-conflict agent round: rebase_pending (and the counted round) are
    restored instead of the task losing its PR and place in the rebase queue."""
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/demo/pull/1"
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["rebase_pending"] = True
    st["rebase_base"] = "main"
    st["rebase_files"] = ["README.md"]
    st["rebase_hunks"] = {"README.md": "<<<<<<< HEAD\n=======\n>>>>>>> main\n"}
    st["rebases"] = 1
    sched.state.save()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()  # dispatch the rebase agent: pops rebase_pending, bumps rebases
    rep = sched.tick()  # reap: the spend-limit message comes back
    assert "DM-001 -> changes_requested (env_error: quota)" in rep.transitions
    assert statuses(sched)["DM-001"] == "changes_requested"
    st = sched.state.get("DM-001")
    assert st["rebase_pending"] is True
    assert st["rebases"] == 1
    task = sched.store.task("DM-001")
    assert task.pr == "https://example.com/demo/pull/1"


def test_claude_quota_during_resume_round_restores_the_question_and_pr(sched, fake_github, monkeypatch):
    """A quota hit mid-resume (the continuation `human.answer()` dispatches once a person
    answers a worker's question) must not discard the pending question/session or send the
    task back to ready, which would lose whatever PR the round that asked the question had
    already opened: it goes back to waiting_human with the question and session restored."""
    task = sched.store.task("DM-001")
    task.pr = "https://example.com/demo/pull/1"
    task.status = Status.WAITING_HUMAN
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["question"] = "Postgres or SQLite?"
    st["session_id"] = "sess-42"
    st["session_harness"] = "claude"
    sched.state.save()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    run = sched.answer(task, "SQLite, single file.")
    assert run.mode == "resume" and run.session_id == "sess-42"
    rep = sched.tick()  # reap: the resume run hits the spend limit instead of finishing
    assert "DM-001 -> waiting_human (env_error: quota)" in rep.transitions
    assert statuses(sched)["DM-001"] == "waiting_human"
    st = sched.state.get("DM-001")
    assert st["question"] == "Postgres or SQLite?"  # restored, not lost
    assert st["session_id"] == "sess-42"
    task = sched.store.task("DM-001")
    assert task.pr == "https://example.com/demo/pull/1"  # unaffected by the harness's own trouble
    assert sched.is_harness_paused("claude")


def test_needs_input_quota_during_resume_still_resumes_cleanly_once_the_harness_recovers(sched, fake_github, monkeypatch):
    """The full loop: work asks a question, the person answers, the answer's resume hits the
    spend limit and is restored to waiting_human, then answering again after the harness
    recovers finishes the task normally."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched.tick()  # dispatch work
    rep = sched.tick()  # reap: the worker asks a question
    assert "DM-001 -> waiting_human" in rep.transitions

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.answer(sched.store.task("DM-001"), "SQLite, single file.")
    rep = sched.tick()  # reap: the resume run hits the spend limit
    assert "DM-001 -> waiting_human (env_error: quota)" in rep.transitions
    assert statuses(sched)["DM-001"] == "waiting_human"
    assert sched.state.get("DM-001")["question"] == "Postgres or SQLite?"

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")  # the account recovered; nocommit protects the probe
    # itself (its throwaway cwd is not a git repo, so a mode that tries to commit would fail)
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    sched.tick()  # probe resumes claude
    assert not sched.is_harness_paused("claude")

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")  # the resumed round finishes normally this time
    run = sched.answer(sched.store.task("DM-001"), "SQLite, single file.")
    assert run.mode == "resume" and run.session_id == "sess-42"
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"


def test_codex_usage_limit_is_also_a_quota_env_error(sched, fake_github, monkeypatch):
    sched.cfg.data["worker_env"]["pass"].append("FAKE_CODEX_*")
    task = sched.store.task("DM-001")
    task.harness = "codex"
    sched.store.save(task)
    monkeypatch.setenv("FAKE_CODEX_MODE", "quota")
    sched.tick()
    rep = sched.tick()
    assert "DM-001 -> ready (env_error: quota)" in rep.transitions
    assert sched.is_harness_paused("codex")
    assert not sched.is_harness_paused("claude")


# ---- quota during automated review, persona and trial rounds (CG-212 revision) -----------
# A quota hit is not special to a work/revise/rebase round: the same account limit can be
# hit mid-review, mid-persona-review or mid-trial (the Codex-trial incident this task cites).
# Each of these dispatches its own claude/codex call outside dispatch_ready's queue, so each
# needs its own gate (is_harness_paused before dispatching) and its own env_error handling
# (pause instead of an ordinary failure) — see _raise_if_harness_paused and
# _pause_for_env_error in scheduler/quota.py.

def test_automated_review_env_error_pauses_the_harness_and_retries_the_round(sched, fake_github, monkeypatch):
    # `sched.state` is replaced by a fresh read at the start of every tick() (so the CLI, web
    # UI or TUI can hand it work between passes): `state.get(...)` is re-fetched after each
    # tick below rather than reused, the same way the rest of this file does.
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()  # dispatch work (normal)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()  # reap work -> push -> PR opened -> review dispatched (its own call hits quota)
    st = sched.state.get("DM-001")
    assert st.get("review_run")
    assert st.get("review_rounds") == 1
    rep = sched.tick()  # reap_review collects the quota output
    assert sched.is_harness_paused("claude")
    st = sched.state.get("DM-001")
    assert st.get("review_run") == ""
    assert st.get("review_rounds") == 0  # the round dispatch counted is given back
    pending = st.get("pending_reviews") or []
    assert any(p.get("kind") == "review" for p in pending)
    assert any("review paused" in t for t in rep.transitions)
    assert statuses(sched)["DM-001"] == "in_review"  # unaffected by the harness's own trouble

    # "nocommit" (not the default "done"): the probe's cwd is not a git repo, so a mode that
    # tries to commit fails the probe itself; the review round is protected either way, since
    # its brief carries the `GARDEN_REVIEW:` marker the fake harness matches regardless of
    # FAKE_CLAUDE_MODE (see handle() in fake_claude.py).
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}  # probe on every tick, for the test
    sched.tick()  # probe resumes claude; dispatch drains the pending review in the same pass
    assert not sched.is_harness_paused("claude")
    st = sched.state.get("DM-001")
    assert st.get("review_run")
    assert st.get("review_rounds") == 1


def test_configured_persona_env_error_pauses_the_harness_and_retries(sched, fake_github, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": False, "personas": ["user"], "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()  # dispatch work (normal)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    rep = sched.tick()  # reap work -> push -> PR opened -> persona dispatched (its own call hits quota)
    assert "DM-001(persona:user)" in rep.dispatched
    rep2 = sched.tick()  # reap_aux collects the quota output
    assert sched.is_harness_paused("claude")
    st = sched.state.get("DM-001")
    pending = st.get("pending_reviews") or []
    assert any(p.get("kind") == "persona" and p.get("name") == "user" for p in pending)
    assert any("persona paused" in t for t in rep2.transitions)
    assert statuses(sched)["DM-001"] == "in_review"

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")  # protects the probe; the persona
    # round is protected by its own `GARDEN_PERSONA:` brief marker regardless
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    rep3 = sched.tick()
    assert not sched.is_harness_paused("claude")
    assert "DM-001(persona:user)" in rep3.dispatched


def test_trial_contender_env_error_pauses_the_harness_and_is_redispatched_on_resume(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.start_trial(sched.store.task("DM-001"), ["claude:sonnet", "claude:opus"])
    rep = sched.tick()  # reap_trial: both contenders hit the same account limit
    assert sched.is_harness_paused("claude")
    contenders = sched.state.get("DM-001")["trial"]["contenders"]
    assert all(c["status"] == "paused" for c in contenders)
    assert any("quota" in (c.get("note") or "") for c in contenders)
    assert rep.dispatched == []  # not treated as an ordinary contender failure

    # "nocommit" only for the probe tick: its cwd is not a git repo, so a mode that tries to
    # commit fails the probe itself. The redispatched contenders need a real commit in their
    # own worktree to reach a PR, so the mode reverts to the default ("done") once the probe
    # (which lags a tick behind the contender redispatch: reap runs before harness_probe, so
    # nothing dispatches to the still-paused harness in the probe's own tick) has run.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")
    monkeypatch.setenv("FAKE_CLAUDE_WINNER", "claude:opus")
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    sched.tick()  # the probe resumes claude (reap already ran this tick with it still paused)
    assert not sched.is_harness_paused("claude")
    contenders = sched.state.get("DM-001")["trial"]["contenders"]
    assert all(c["status"] == "paused" for c in contenders)  # redispatch waits for the next reap

    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    sched.tick()  # reap_trial redispatches both contenders now that claude is up
    contenders = sched.state.get("DM-001")["trial"]["contenders"]
    assert all(c["status"] == "running" for c in contenders)

    sched.tick()  # reap_trial finalizes the redispatched runs and starts the comparison
    assert sched.state.get("DM-001")["trial"]["status"] == "comparing"

    sched.tick()  # reap_aux collects the comparison verdict
    trial = sched.state.get("DM-001")["trial"]
    assert trial["status"] == "done"
    assert trial["winner"] == "claude:opus"


def test_trial_compare_env_error_pauses_the_harness_and_is_retried_on_resume(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WINNER", "claude:opus")
    sched.start_trial(sched.store.task("DM-001"), ["claude:sonnet", "claude:opus"])
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    rep = sched.tick()  # reap_trial: both contenders finish normally, comparison dispatched (hits quota)
    assert sched.state.get("DM-001")["trial"]["status"] == "comparing"
    assert "DM-001(compare)" in rep.dispatched
    rep2 = sched.tick()  # reap_aux collects the quota output
    assert sched.is_harness_paused("claude")
    trial = sched.state.get("DM-001")["trial"]
    assert trial["status"] == "running"
    assert trial["compare_paused"] is True
    assert any("compare paused" in t for t in rep2.transitions)

    # "nocommit" (not the default "done"): the probe's cwd is not a git repo. The retried
    # comparison is protected by its own `GARDEN_COMPARE:` brief marker regardless.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    sched.tick()  # probe resumes claude, too late for this tick's reap_trial pass
    assert not sched.is_harness_paused("claude")

    rep3 = sched.tick()  # reap_trial retries the comparison now that claude is up
    assert "DM-001(compare)" in rep3.dispatched
    sched.tick()  # reap_aux collects the real verdict
    trial = sched.state.get("DM-001")["trial"]
    assert trial["status"] == "done"
    assert trial["winner"] == "claude:opus"


def test_start_trial_refuses_a_paused_harness(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()  # DM-001 back to ready, claude paused
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    with pytest.raises(RuntimeError, match="paused"):
        sched.start_trial(sched.store.task("DM-001"), ["claude:sonnet", "claude:opus"])


def test_direct_review_and_aux_dispatch_refuse_a_paused_harness(sched, monkeypatch):
    """A human-triggered round (`garden review`, `garden persona`, a trial's comparison)
    gets the same refusal a fresh work/revise/rebase dispatch would, rather than starting a
    run that can only hit the same account limit again."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    sched.tick()
    sched.tick()  # DM-001 back to ready, claude paused
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    task = sched.store.task("DM-001")
    for attempt in (lambda: sched.dispatch_review(task),
                    lambda: sched.dispatch_persona_pr(task, "security"),
                    lambda: sched.dispatch_aux("compare", task, "brief", sched.worktree_for(task), {})):
        with pytest.raises(RuntimeError, match="paused"):
            attempt()
