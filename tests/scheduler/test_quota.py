"""A quota/spend-limit error from a harness pauses dispatch for that harness and returns
the task to ready without burning an attempt (CG-212); a cheap probe resumes it later."""

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
