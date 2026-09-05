"""The five coordination features: event log, pause/resume, discovered work, stall + budgets, stacking."""

import subprocess

from garden.events import EventLog, digest, metrics, parse_since
from garden.github import Feedback
from garden.model import Status


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def gitc(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


# ---- 5. event log ------------------------------------------------------------
def test_event_log_digest_and_metrics(sched, fake_github, tmp_path):
    sched.tick()
    sched.tick()
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    sched.tick()
    log = EventLog(sched.cfg.garden_dir / "events.jsonl")
    kinds = [e["kind"] for e in log.read(task_id="DM-001")]
    assert kinds[:3] == ["dispatch", "transition", "run_finished"]
    assert "pr_opened" in kinds and kinds.count("transition") >= 3
    d = digest(log.read())
    # DM-002 is dispatched (stacked on DM-001) in the second tick and, workers being in
    # process, has finished by the third, which reaps it into its own PR before the merge.
    assert [e["task"] for e in d["prs_opened"]] == ["DM-001", "DM-002"]
    assert [e["task"] for e in d["merged"]] == ["DM-001"]
    assert d["cost_usd"] == 0.10 and d["dispatched"] == 2
    m = metrics(log.read(), sched.store.tasks())
    row = next(r for r in m["tasks"] if r["id"] == "DM-001")
    assert row["runs"] == 1 and row["lead_hours"] is not None and row["cost_usd"] == 0.05
    assert m["by_difficulty"]["medium"]["done"] == 1
    assert parse_since("24h") < parse_since("1m")
    empty = EventLog(tmp_path / "nope.jsonl")
    assert empty.read() == []


# ---- 2. pause and resume -----------------------------------------------------
def test_needs_input_waits_then_resumes_same_session(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched.tick()
    rep = sched.tick()
    assert "DM-001 -> waiting_human" in rep.transitions
    st = sched.state.get("DM-001")
    assert st["question"] == "Postgres or SQLite?" and st["session_id"] == "sess-42"
    assert sched.slots_free() == 2  # a waiting task holds no slot
    # nothing dispatches while waiting; retry-ready DM-002 is still blocked
    rep = sched.tick()
    assert rep.dispatched == []
    run = sched.answer(sched.store.task("DM-001"), "SQLite, single file.")
    assert run.mode == "resume" and run.session_id == "sess-42"
    assert "SQLite, single file." in (run.path / "brief.md").read_text()
    assert "--resume sess-42" in (run.path / "command.txt").read_text()
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    assert (sched.worktree_for(sched.store.task("DM-001")) / "resumed.txt").exists()
    assert (sched.worktree_for(sched.store.task("DM-001")) / "partial.txt").exists()  # earlier commit kept
    assert sched.store.task("DM-001").attempts == 1  # resume is not a new attempt
    assert sched.state.get("DM-001")["qa"][0]["a"] == "SQLite, single file."
    kinds = [e["kind"] for e in EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001")]
    assert "waiting_human" in kinds and "answer" in kinds


def test_answer_without_resume_support_redispatches_with_qa(sched, monkeypatch):
    sched.cfg.data["harnesses"]["claude"]["resume"] = False
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched.tick()
    sched.tick()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    run = sched.answer(sched.store.task("DM-001"), "Use SQLite.")
    assert run.session_id == "" and "## Answers from the human" in (run.path / "brief.md").read_text()
    assert "Use SQLite." in (run.path / "brief.md").read_text()


# ---- 3. discovered work ------------------------------------------------------
def test_discovered_work_files_tasks(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover")
    sched.tick()
    rep = sched.tick()
    sched.store.invalidate()
    tasks = sched.store.tasks()
    new = sorted(t for t in tasks if tasks[t].discovered_from == "DM-001")
    assert len(new) == 2  # the duplicate title was skipped
    by_title = {tasks[t].title: tasks[t] for t in new}
    flaky, schema = by_title["Fix the flaky widget test"], by_title["Add the missing config schema"]
    assert flaky.status == Status.DRAFT and flaky.difficulty == "easy"
    assert schema.status == Status.RUNNING and "Provenance" in schema.body  # blocking -> ready -> dispatched this tick
    assert "Discovered work" in fake_github.created[0]["body"]
    assert f"{schema.id}(work)" in rep.dispatched
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(kinds=["discovered"])
    assert {e["new_task"] for e in evs} == set(new)


# ---- diff summary on run records (CG-115) -------------------------------------
def test_run_records_diff_stat_on_finish(sched, fake_github):
    sched.tick()
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.mode == "work" and run.status == "done"
    assert "worker-output.txt" in run.diff_stat
    assert "files changed" in run.diff_stat


# ---- 4. stall detection and budgets -----------------------------------------
def test_stall_when_revise_changes_nothing(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nochange")
    sched.cfg.data["max_revisions"] = 4  # allow enough rounds for two revises before stall + one after retry
    sched.tick()
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]

    # First revise: body changes from the work-run body to "b"; diff unchanged.
    # The round is NOT a stall because the description did change.
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "please fix", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]
    fake_github.feedback.clear()
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"  # not stalled yet

    # Second revise: body "b" again (same), diff still unchanged -> stall.
    pr.updated_at = "t3"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "same again", "created": "2099-01-02T00:00:00Z"}])
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]
    rep = sched.tick()
    assert "DM-001 stalled" in rep.transitions
    assert statuses(sched)["DM-001"] == "changes_requested"
    stop = sched.state.get("DM-001")["needs_human"]
    assert stop["kind"] == "stall" and "no change" in stop["reason"]
    assert stop["prior_status"] == "in_review"
    assert "garden triage DM-001" in sched.store.task("DM-001").body
    # more feedback does not spend another round while a human is needed
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "again", "created": "2099-01-03T00:00:00Z"}])
    pr.updated_at = "t4"
    rep = sched.tick()
    assert rep.dispatched == []
    # a human resets; the loop continues
    sched.retry(sched.store.task("DM-001"))
    assert "needs_human" not in sched.state.get("DM-001")
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]


def test_stall_description_only_round_not_stalled(sched, fake_github, monkeypatch):
    """A revise round that only changes the PR body is not a stall."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nochange")
    sched.tick()
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "fix the description", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()
    fake_github.feedback.clear()
    sched.tick()
    # nochange revise: no new commits (diff hash same as work run) but body changed to "b"
    assert statuses(sched)["DM-001"] == "in_review"
    assert not sched.state.get("DM-001").get("needs_human")


def test_repeated_review_finding_stalls(sched, fake_github, monkeypatch):
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 3, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    sched.tick()  # PR + review dispatched
    rep = sched.tick()  # review -> request_changes -> revise
    assert "DM-001(revise)" in rep.dispatched
    sched.tick()  # revise reaped -> second review dispatched
    rep = sched.tick()  # same blocking finding again -> stall
    assert "DM-001 stalled" in rep.transitions
    stop = sched.state.get("DM-001")["needs_human"]
    assert stop["kind"] == "stall" and "repeated" in stop["reason"]


def test_review_parallel_queues_and_drains(sched, fake_github):
    """review_parallel=1 with two PRs pushed in the same tick: only one review starts, the
    other is queued and dispatched once the first review run is reaped (not blocked by
    max_parallel, which stays at the fixture's 2)."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.cfg.data["review_parallel"] = 1
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    sched.tick()  # dispatch DM-001 and DM-002 (work); max_parallel=2
    rep = sched.tick()  # both work runs reap and push PRs; only one review slot is free
    reviewed = [d.split("(")[0] for d in rep.dispatched if d.endswith("(review)")]
    assert len(reviewed) == 1
    deferred = "DM-002" if reviewed[0] == "DM-001" else "DM-001"
    assert sched.state.get(deferred)["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert sched.review_slots_free() == 0
    rep = sched.tick()  # the first review is reaped, freeing the slot for the deferred one
    assert f"{deferred}(review)" in rep.dispatched
    assert not sched.state.get(deferred).get("pending_reviews")


def test_phase_budget_pauses_dispatch(sched):
    sched.cfg.data["budgets"] = {"demo/p1": 0.04}
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    sched.tick()  # each run cost 0.05 -> budget exceeded
    assert sched.spent_for("demo/p1") == 0.10
    for t in sched.store.tasks().values():
        sched.retry(t)
    rep = sched.tick()
    assert rep.dispatched == []
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(kinds=["budget"])
    assert len(evs) == 1 and evs[0]["phase"] == "demo/p1"
    sched.cfg.data["budgets"] = {"demo/p1": 1.0}
    rep = sched.tick()
    assert len(rep.dispatched) == 2


def test_set_budget_override_reloads_without_restart(sched):
    # A cap in config pauses dispatch once spend passes it.
    sched.cfg.data["budgets"] = {"demo/p1": 0.04}
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    sched.tick()  # each run cost 0.05 -> exceeded, dispatch paused
    for t in sched.store.tasks().values():
        sched.retry(t)
    assert sched.tick().dispatched == []
    assert sched.state.get("_phase:demo/p1").get("budget_hit")
    # Raise the cap through the shared code path (what the web route and CLI call). No config
    # edit and no restart: the next tick re-reads state and resumes.
    sched.set_budget("demo/p1", 100.0)
    assert sched.budget_for("demo/p1") == 100.0
    assert not sched.state.get("_phase:demo/p1").get("budget_hit")  # pause marker cleared
    assert len(sched.tick().dispatched) == 2


def test_set_budget_none_removes_cap(sched):
    sched.cfg.data["budgets"] = {"demo/p1": 0.04}
    # "no budget" overrides a configured cap: budget_for reports no cap and it never pauses.
    sched.set_budget("demo/p1", None)
    assert sched.budget_for("demo/p1") == 0.0
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    sched.tick()  # reap the first batch so its finished runs free their slots
    for t in sched.store.tasks().values():
        sched.retry(t)
    assert len(sched.tick().dispatched) == 2  # still dispatching despite the config cap


# ---- 1. stacked dependencies -------------------------------------------------
def test_stacked_dispatch_and_restack_on_merge(sched, fake_github, tmp_path):
    sched.tick()
    rep = sched.tick()  # DM-001 in_review with an open PR -> DM-002 stacks on it in the same tick
    assert "DM-002(work)" in rep.dispatched
    st2 = sched.state.get("DM-002")
    assert st2["stack_parent"] == "DM-001" and st2["pr_base"] == "garden/dm-001-first-task"
    run = sched.runs.latest("DM-002")
    assert run.base == "garden/dm-001-first-task"
    brief = (run.path / "brief.md").read_text()
    assert "## Stacked branch" in brief and "DM-001" in brief
    wt2 = sched.worktree_for(sched.store.task("DM-002"))
    assert (wt2 / "worker-output.txt").exists()  # parent's work is present in the child's worktree
    sched.tick()
    assert fake_github.created[1]["base"] == "garden/dm-001-first-task"
    assert "Stacked on `DM-001`" in fake_github.created[1]["body"]
    # parent merges (simulate GitHub merging the branch into main) -> child retargeted and rebased
    repo = tmp_path / "repo"
    gitc("fetch", "origin", cwd=repo)
    gitc("merge", "-q", "--ff-only", "origin/garden/dm-001-first-task", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    assert "DM-001 -> done" in rep.transitions and "DM-002 restacked onto main" in rep.transitions
    assert fake_github.updated[-1]["base"] == "main"
    st2 = sched.state.get("DM-002")
    assert "stack_parent" not in st2 and st2["pr_base"] == "main"
    ahead = gitc("rev-list", "--count", "origin/main..origin/garden/dm-002-second-task", cwd=repo).strip()
    assert ahead == "1"  # only the child's own commit on top of main
    s = statuses(sched)
    assert s["DM-001"] == "done" and s["DM-002"] == "in_review"


def test_stacked_child_automerges_only_after_restack(sched, fake_github, tmp_path):
    """A stacked child with every other gate green waits for the restack, then automerges."""
    sched.cfg.data["github"]["automerge"] = True
    sched.tick()
    sched.tick()  # DM-001 in_review; DM-002 stacks on it and dispatches
    sched.tick()  # DM-002's PR opens targeting DM-001's branch
    child_branch = "garden/dm-002-second-task"
    st2 = sched.state.get("DM-002")
    assert st2["stack_parent"] == "DM-001" and st2["pr_base"] == "garden/dm-001-first-task"
    st2["last_review"] = {"verdict": "approve", "summary": "ok"}
    st2["last_review_run"] = "rev-2"
    st2["review_rounds"] = 1
    sched.state.save()
    pr2 = fake_github.prs[child_branch]
    pr2.mergeable = "MERGEABLE"
    pr2.checks = "SUCCESS"

    # every gate green but the base is the parent's branch: held, not merged, reason names the parent
    sched.tick()
    assert {"number": pr2.number} not in [{"number": m["number"]} for m in fake_github.merged]
    assert pr2.state == "OPEN"
    assert sched.state.get("DM-002").get("automerge_blocked") == "stacked on DM-001; waits for the restack"

    # the parent merges: DM-002 is retargeted to main and rebased
    repo = tmp_path / "repo"
    gitc("fetch", "origin", cwd=repo)
    gitc("merge", "-q", "--ff-only", "origin/garden/dm-001-first-task", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    assert "DM-002 restacked onto main" in rep.transitions
    assert sched.state.get("DM-002")["pr_base"] == "main" and pr2.base == "main"

    # now that its base is the product base, the next poll automerges it
    sched.tick()
    assert pr2.state == "MERGED"
    assert sched.state.get("DM-002").get("automerged")


def test_restack_keeps_remote_only_commits(sched, fake_github, tmp_path):
    """A rebase round on the restack path folds in commits pushed only to the remote branch."""
    sched.tick()
    sched.tick()  # DM-001 in_review; DM-002 stacks
    sched.tick()  # DM-002's PR opens targeting DM-001's branch
    child_branch = "garden/dm-002-second-task"

    # seed a commit that exists only on the remote child branch (as if merged into it)
    rc = tmp_path / "remote-clone"
    gitc("fetch", "origin", cwd=rc)
    gitc("checkout", "-B", "child", f"origin/{child_branch}", cwd=rc)
    (rc / "remote-only.txt").write_text("only on the remote\n")
    gitc("add", "-A", cwd=rc)
    gitc("commit", "-q", "-m", "remote-only commit", cwd=rc)
    gitc("push", "-q", "origin", f"child:{child_branch}", cwd=rc)

    # the parent merges -> DM-002 is restacked onto main and force-pushed
    repo = tmp_path / "repo"
    gitc("fetch", "origin", cwd=repo)
    gitc("merge", "-q", "--ff-only", "origin/garden/dm-001-first-task", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    assert "DM-002 restacked onto main" in rep.transitions

    # the remote-only commit survived the rebase and force-push
    gitc("fetch", "origin", cwd=repo)
    files = gitc("ls-tree", "-r", "--name-only", f"origin/{child_branch}", cwd=repo)
    assert "remote-only.txt" in files


def test_restack_conflict_dispatches_rebase_agent(sched, fake_github, tmp_path, monkeypatch):
    """A restack whose rebase conflicts textually dispatches an easy-tier rebase agent, not a
    full revise run: the rebase brief carries only the hunks, and max_revisions is untouched."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "conflict")  # both tasks rewrite README.md
    sched.tick()
    sched.tick()  # DM-001 PR; DM-002 stacked
    sched.tick()  # DM-002 PR targeting DM-001's branch
    repo = tmp_path / "repo"
    # main moves on with a conflicting README change *and* the parent merges (squash-style: different history)
    (repo / "README.md").write_text("# demo\n\nchanged on main\n")
    gitc("commit", "-qam", "main edit", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    rep = sched.tick()
    assert "DM-002 -> changes_requested (rebase)" in rep.transitions
    assert "DM-002(rebase)" in rep.dispatched  # an easy-tier rebase agent, in the same tick
    run = sched.runs.latest("DM-002")
    assert run.mode == "rebase" and run.difficulty == "easy"
    brief = (run.path / "brief.md").read_text()
    assert "git fetch origin && git rebase origin/main" in brief and "README.md" in brief
    assert "Resolve the conflict, change nothing else" in brief
    assert "## Reading list" not in brief  # the minimal rebase brief, not the full worker brief
    assert sched.state.get("DM-002")["force_push"] is True  # the rebased branch will be force-pushed
    assert sched.state.get("DM-002").get("rebases") == 1  # its own counter
    assert int(sched.state.get("DM-002").get("revisions", 0)) == 0  # never touches max_revisions


# ---- CG-173: a merging parent never orphans its stacked children -------------
def test_merge_retargets_children_before_deleting_branch(sched, fake_github, tmp_path):
    """When a stack parent automerges, every open stacked-child PR is retargeted to the final
    base before the parent's branch is deleted, so GitHub never closes the child mid-merge."""
    sched.cfg.data["github"]["automerge"] = True
    sched.tick()
    sched.tick()  # DM-001 in_review + PR; DM-002 stacks and dispatches
    sched.tick()  # DM-002's PR opens targeting DM-001's branch
    parent_branch, child_branch = "garden/dm-001-first-task", "garden/dm-002-second-task"
    assert fake_github.prs[child_branch].base == parent_branch

    # DM-001's gates go green so the merge queue takes it
    st1 = sched.state.get("DM-001")
    st1["last_review"] = {"verdict": "approve", "summary": "ok"}
    st1["last_review_run"] = "rev-1"
    st1["review_rounds"] = 1
    sched.state.save()
    pr1 = fake_github.prs[parent_branch]
    pr1.mergeable, pr1.checks = "MERGEABLE", "SUCCESS"

    sched.tick()  # merge queue merges DM-001
    assert pr1.state == "MERGED"
    assert fake_github.merged[-1] == {"number": pr1.number, "method": "squash", "delete_branch": True}
    # the child PR was retargeted to main *before* the parent's branch was deleted, so it survives
    child = fake_github.prs[child_branch]
    assert child.base == "main"
    assert {"number": child.number, "base": "main"} in [{"number": u["number"], "base": u["base"]} for u in fake_github.updated]
    assert child.state == "OPEN"


def test_child_run_finishing_after_parent_merged_opens_pr_on_final_base(sched, fake_github, tmp_path):
    """A child whose stack parent became terminal while its work run was in flight opens its PR
    against the final base and rebases onto it, never against the parent's (deleted) branch."""
    sched.tick()
    sched.tick()  # DM-001 in_review + PR; DM-002 stacks, dispatches and its in-process run finishes
    child_branch = "garden/dm-002-second-task"
    assert statuses(sched)["DM-002"] == "running"
    st2 = sched.state.get("DM-002")
    assert st2["stack_parent"] == "DM-001" and st2["pr_base"] == "garden/dm-001-first-task"
    assert child_branch not in fake_github.prs  # the child has not opened a PR yet

    # the parent merges into main before the child's run is reaped
    repo = tmp_path / "repo"
    gitc("fetch", "origin", cwd=repo)
    gitc("merge", "-q", "--ff-only", "origin/garden/dm-001-first-task", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    t1 = sched.store.task("DM-001")
    t1.status = Status.DONE
    sched.store.save(t1)

    rep = sched.tick()  # reap DM-002: restack onto main, then open its PR against main
    assert "DM-002 restacked onto main" in rep.transitions
    created = [c for c in fake_github.created if c["head"] == child_branch]
    assert created and created[0]["base"] == "main"  # the PR opened against the final base
    assert fake_github.prs[child_branch].base == "main"
    st2 = sched.state.get("DM-002")
    assert "stack_parent" not in st2 and st2["pr_base"] == "main"
    assert statuses(sched)["DM-002"] == "in_review"
    gitc("fetch", "origin", cwd=repo)
    ahead = gitc("rev-list", "--count", "origin/main..origin/garden/dm-002-second-task", cwd=repo).strip()
    assert ahead == "1"  # only the child's own commit on top of main


def _stacked_child_with_closed_base(sched, fake_github, tmp_path):
    """Drive DM-002 to an open PR stacked on DM-001, then simulate GitHub closing that PR because
    the parent's branch was deleted (base_ref_deleted). DM-001 is marked done in place so it does
    not re-run `_on_merged` during the poll under test."""
    sched.tick()
    sched.tick()  # DM-001 in_review + PR; DM-002 stacks and dispatches
    sched.tick()  # DM-002's PR opens targeting DM-001's branch
    child_branch = "garden/dm-002-second-task"
    assert fake_github.prs[child_branch].base == "garden/dm-001-first-task"

    repo = tmp_path / "repo"
    gitc("fetch", "origin", cwd=repo)
    gitc("merge", "-q", "--ff-only", "origin/garden/dm-001-first-task", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    t1 = sched.store.task("DM-001")
    t1.status = Status.DONE
    sched.store.save(t1)
    child = fake_github.prs[child_branch]
    # Deleting the merged parent's branch closes the child PR (base_ref_deleted), the way real
    # GitHub does (CG-173); the fake models it rather than the test setting the flags by hand.
    fake_github.delete_branch("test/demo", "garden/dm-001-first-task")
    assert child.state == "CLOSED" and child.number in fake_github.base_deleted
    return child


def test_child_pr_closed_by_base_deletion_is_reopened(sched, fake_github, tmp_path):
    """A child PR GitHub closed because its base branch was deleted is reopened onto the final
    base and rebased, not failed."""
    child = _stacked_child_with_closed_base(sched, fake_github, tmp_path)
    rep = sched.tick()  # poll DM-002: recognise the base deletion and reopen onto main
    assert child.number in fake_github.reopened
    assert child.state == "OPEN" and child.base == "main"
    assert statuses(sched)["DM-002"] == "in_review"  # not failed
    assert "DM-002 -> failed (PR closed)" not in rep.transitions
    assert sched.state.get("DM-002")["pr_base"] == "main"


def test_child_pr_recreated_when_github_refuses_reopen(sched, fake_github, tmp_path):
    """When GitHub refuses to reopen the closed child PR, a fresh PR is opened from the same
    branch against the final base."""
    child = _stacked_child_with_closed_base(sched, fake_github, tmp_path)
    old_number = child.number
    fake_github.refuse_reopen.add(old_number)
    sched.tick()  # poll DM-002: reopen refused, so a new PR is opened from the same branch
    reopened = fake_github.prs["garden/dm-002-second-task"]
    assert reopened.number != old_number and reopened.state == "OPEN" and reopened.base == "main"
    assert sched.state.get("DM-002")["pr_number"] == reopened.number
    assert statuses(sched)["DM-002"] == "in_review"


# ---- 6. conflict detection ---------------------------------------------------
def test_conflicting_pr_dispatches_rebase_agent(sched, fake_github, tmp_path, monkeypatch):
    """When GitHub reports a PR as CONFLICTING and the actual rebase conflicts textually,
    poll dispatches an easy-tier rebase agent carrying only the conflicting hunks."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "conflict")  # worker writes README.md
    sched.tick()
    sched.tick()  # DM-001 -> in_review
    assert statuses(sched)["DM-001"] == "in_review"

    # main diverges with a conflicting change to the same file
    repo = tmp_path / "repo"
    (repo / "README.md").write_text("# demo\n\nchanged on main\n")
    gitc("commit", "-qam", "main conflicts with worker", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)

    fake_github.prs["garden/dm-001-first-task"].mergeable = "CONFLICTING"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    rep = sched.tick()

    assert "DM-001 -> changes_requested (rebase)" in rep.transitions
    assert "DM-001(rebase)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "rebase" and run.difficulty == "easy"
    brief = (run.path / "brief.md").read_text()
    assert "Resolve the conflict, change nothing else" in brief
    assert "README.md" in brief  # the conflicting hunk is carried in the brief
    # force_push is set for the upcoming push after the agent resolves the conflict
    assert sched.state.get("DM-001").get("force_push") is True
    assert int(sched.state.get("DM-001").get("revisions", 0)) == 0  # a rebase never counts as a revise
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["rebase"])
    assert any(e.get("resolved") is False and "README.md" in (e.get("files") or []) for e in evs)


def test_conflicting_pr_auto_rebased_when_clean(sched, fake_github, tmp_path):
    """When GitHub reports CONFLICTING but the actual rebase applies cleanly, poll rebases
    mechanically (no model) as its own `rebase` run, force-pushes, and queues no agent."""
    sched.tick()
    sched.tick()  # DM-001 -> in_review
    assert statuses(sched)["DM-001"] == "in_review"

    # main advances with a non-conflicting change (a new file the worker never touched)
    repo = tmp_path / "repo"
    (repo / "other.txt").write_text("unrelated change\n")
    gitc("add", "other.txt", cwd=repo)
    gitc("commit", "-q", "-m", "main adds unrelated file", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)

    fake_github.prs["garden/dm-001-first-task"].mergeable = "CONFLICTING"
    # a pre-PR check that leaves a marker, so we can prove the round re-ran the checks
    marker = tmp_path / "clean-rebase-check-ran"
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": f"touch {marker}"}], "ci": []}
    rep = sched.tick()  # mechanical rebase + push; the pre-PR checks start as a detached run
    fake_github.prs["garden/dm-001-first-task"].mergeable = "MERGEABLE"  # GitHub recomputes after the push
    sched.tick()  # reap the check run: diff unchanged -> verdict kept, check event emitted

    # clean rebase: no agent, task stays in_review, pending_feedback is empty
    assert statuses(sched)["DM-001"] == "in_review"
    assert "DM-001(rebase)" not in rep.dispatched and "DM-001(revise)" not in rep.dispatched
    assert not sched.state.get("DM-001").get("pending_feedback")
    # a rebase run record was written with no harness call, and its own counter moved
    rb = [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]
    assert len(rb) == 1 and rb[0].harness == "" and rb[0].status == "done" and rb[0].cost_usd == 0.0
    assert sched.state.get("DM-001").get("rebases") == 1
    # the pre-PR checks were re-run after the mechanical rebase, as a detached check run
    assert marker.exists()
    checks = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["check"])
    assert any(e.get("stage") == "pre_pr" and e.get("name") == "unit" for e in checks)
    # rebase event recorded as resolved
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["rebase"])
    assert any(e.get("resolved") is True for e in evs)
    # origin/main is now an ancestor of the branch (rebase completed)
    gitc("fetch", "origin", cwd=repo)
    branch = sched.store.task("DM-001").branch
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", f"origin/{branch}"],
                       cwd=repo, capture_output=True)
    assert r.returncode == 0, "origin/main must be an ancestor of the branch after rebase"


def test_stack_disabled_keeps_strict_blocking(sched, fake_github):
    sched.cfg.data["stack"] = False
    sched.tick()
    rep = sched.tick()
    assert "DM-002(work)" not in rep.dispatched
    assert statuses(sched)["DM-002"] == "ready"
    from garden.graph import effective_status

    assert effective_status(sched.store.task("DM-002"), sched.store.tasks(), stack=False) == "blocked"
    assert effective_status(sched.store.task("DM-002"), sched.store.tasks(), stack=True) == "ready"
