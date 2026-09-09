"""What a person does to a task: retry past the cap, retry a capped pre-PR round."""

import subprocess
import sys

import pytest

from garden import gitops
from garden.github import GitHubError
from garden.model import Status, now_iso
from garden.preflight import PREFLIGHT_ITEMS
from garden.runner.manual import ManualRunner
from garden.scheduler import Scheduler
from garden.store import Store
from tests.scheduler.conftest import statuses


def test_manual_reservation_is_retry_safe_and_suppresses_dispatch(sched):
    task = sched.store.task("DM-001")
    first = sched.reserve_manual(task, actor="operator", note="repairing directly")
    again = sched.reserve_manual(task, actor="operator", note="repairing directly")

    assert again == first
    assert sched.tick().dispatched == []
    assert statuses(sched)[task.id] == "ready"
    assert sched.state.get(task.id)["manual_reservation"]["actor"] == "operator"
    assert "Manual mode reserved by operator" in sched.store.task(task.id).body

    sched.return_to_automation(
        sched.store.task(task.id), reservation_id=first["id"], expected_head=""
    )
    assert sched.manual_reservation(task) is None
    assert "DM-001(work)" in sched.tick().dispatched


def test_manual_reservation_does_not_interrupt_active_work_and_return_is_guarded(sched):
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", "work")
    task.status = Status.RUNNING
    sched.store.save(task)
    reservation = sched.reserve_manual(task, actor="human_owner")

    assert sched.runs.runs_for(task.id)[-1].run_id == run.run_id
    with pytest.raises(RuntimeError, match="still active"):
        sched.return_to_automation(task, reservation_id=reservation["id"])
    with pytest.raises(RuntimeError, match="stale"):
        sched.return_to_automation(task, reservation_id="not-current")


def test_manual_reservation_parks_finished_worker_until_return(sched):
    sched.cfg.data["stack"] = False
    sched.tick()  # dispatch a worker which finishes in-process
    task = sched.store.task("DM-001")
    reservation = sched.reserve_manual(task, actor="operator")

    sched.tick(dispatch=False)

    run = sched.runs.latest(task.id)
    assert run.status == "done"
    assert statuses(sched)[task.id] == "running"
    assert not any(active.task_id == task.id for active in sched.runs.active())

    sched.return_to_automation(
        sched.store.task(task.id), reservation_id=reservation["id"], expected_head=""
    )
    sched.tick(dispatch=False)
    assert statuses(sched)[task.id] == "in_review"


def test_manual_reservation_parks_finished_review_verdict_until_return(sched, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    sched.tick()  # reap work and dispatch the review, which finishes in-process
    task = sched.store.task("DM-001")
    reservation = sched.reserve_manual(task)

    sched.tick(dispatch=False)

    run_id = sched.state.get(task.id)["review_run"]
    run = next(run for run in sched.runs.runs_for(task.id) if run.run_id == run_id)
    assert run.status == "done"
    assert statuses(sched)[task.id] == "in_review"
    assert not sched.state.get(task.id).get("pending_feedback")

    sched.return_to_automation(
        sched.store.task(task.id), reservation_id=reservation["id"],
        expected_head=str(sched.state.get(task.id).get("head_sha") or ""),
    )
    sched.tick(dispatch=False)
    assert statuses(sched)[task.id] == "changes_requested"
    assert sched.state.get(task.id).get("pending_feedback")


def test_manual_reservation_parks_finished_persona_until_return(sched):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": False}
    sched.tick()
    sched.tick()  # open the PR without dispatching an automated review
    task = sched.store.task("DM-001")
    run = sched.dispatch_persona_pr(task, "security")
    reservation = sched.reserve_manual(task)

    sched.tick(dispatch=False)

    assert sched.runs.latest(task.id).status == "done"
    assert not sched.state.get(task.id).get("persona_reviews")

    sched.return_to_automation(
        sched.store.task(task.id), reservation_id=reservation["id"],
        expected_head=str(sched.state.get(task.id).get("head_sha") or ""),
    )
    sched.tick(dispatch=False)
    assert sched.state.get(task.id)["persona_reviews"][-1]["run"] == run.run_id


def test_manual_reservation_guards_every_new_task_run_kind(sched):
    task = sched.store.task("DM-001")
    task.branch = task.default_branch()
    task.pr = "https://example.com/pull/1"
    sched.store.save(task)
    sched.reserve_manual(task)

    for mode in ("work", "revise", "resume", "rebase"):
        with pytest.raises(RuntimeError, match="Manual mode"):
            sched.dispatch(task, mode=mode)
    with pytest.raises(RuntimeError, match="Manual mode"):
        sched.dispatch_review(task)
    with pytest.raises(RuntimeError, match="Manual mode"):
        sched.dispatch_persona_pr(task, "security")
    with pytest.raises(RuntimeError, match="Manual mode"):
        sched.dispatch_edit(task)
    with pytest.raises(RuntimeError, match="Manual mode"):
        sched._dispatch_check_run(
            task, worktree=sched.worktree_for(task), branch=task.branch, base="main",
            specs=[], stage="pre_pr", cont={}, rep=sched.tick(dispatch=False),
        )
    assert sched.mechanical_rebase(task, "main", sched.tick(dispatch=False), reason="test") == "held"


def test_take_manual_refuses_a_stale_ready_task_with_an_active_manual_claim(sched):
    """A manual run owns its task even if an interrupted state write left it READY."""
    task = sched.store.task("DM-001")
    task.runner = "manual"
    sched.store.save(task)
    claimed = sched.runs.new_run(task.id, "manual", "work")

    with pytest.raises(RuntimeError, match="already claimed"):
        sched.take_manual(sched.store.task(task.id))

    assert sched.runs.runs_for(task.id) == [claimed]


def test_retry_grants_one_more_round_past_cap(sched, fake_github):
    """Resuming a capped task rolls the revision counter back one so a revise run runs."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/101"
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions (2) in the test garden
    st["needs_human"] = "2 revision rounds already used"
    sched.retry(t)
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # cap (2) minus one -> one more round dispatchable


def test_retry_of_changes_requested_without_pr_is_a_revise(sched, fake_github):
    """A pre-PR check that failed at the cap leaves the task in changes_requested with no
    PR. `garden retry` must continue the revise loop (keep the feedback, roll the cap back),
    not reset to a fresh work run that would drop both."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = ""  # capped before any PR was opened
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions in the test garden
    st["pending_feedback"] = "- **pre-PR check** `unit` fail: exit 1"
    st["needs_human"] = "pre-PR checks failed and 2 revision rounds already used"
    sched.retry(sched.store.task("DM-001"))
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # one more round dispatchable, counter not lost
    assert st["pending_feedback"]  # feedback preserved, not dropped for a fresh work run
    assert statuses(sched)["DM-001"] == "changes_requested"
    # the revise round is dispatchable now
    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched


def test_delegated_recovery_queues_one_retained_feedback_revision(sched, fake_github):
    """A delegated operator repairs a routine cap without turning it into an owner card.

    The same feedback fingerprint may use exactly one extra round; a repeated unchanged
    failure remains stopped for a product owner instead of consuming another worker run.
    """
    from garden.inbox import build_inbox

    sched.cfg.data["recovery"] = {"delegated": True}
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["revisions"] = 2
    st["pending_feedback"] = "- **CI** is missing; preserve this request"
    st["needs_human"] = {"kind": "revision_cap", "reason": "2 revision rounds used",
                         "delegated_recovery": True}

    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == task.id)
    assert card["group"] == "operator"
    assert any(action["kind"] == "recover" for action in card["actions"])
    assert sched.delegate_recovery(task) == "one retained-feedback revise round queued"
    assert st["pending_feedback"] == "- **CI** is missing; preserve this request"
    assert st["revisions"] == 1
    assert "DM-001(revise)" in sched.tick().dispatched

    task = sched.store.task(task.id)
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["revisions"] = 2
    st["pending_feedback"] = "- **CI** is missing; preserve this request"
    st["needs_human"] = {"kind": "revision_cap", "reason": "2 revision rounds used",
                         "delegated_recovery": True}
    with pytest.raises(RuntimeError, match="unchanged recovery"):
        sched.delegate_recovery(sched.store.task(task.id))


def test_delegated_check_recovery_keeps_stop_when_resource_admission_defers(sched, monkeypatch):
    """A resource gate cannot consume the sole delegated check continuation."""
    from garden.scheduler.resources import ResourcePressureError

    sched.cfg.data["recovery"] = {"delegated": True}
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["needs_human"] = {"kind": "check_did_not_run", "reason": "timed out",
                         "delegated_recovery": True}
    st["recovery_check"] = {"stage": "ci", "specs": [{"name": "unit", "command": "true"}],
                            "cont": {"worktree": str(sched.worktree_for(task)), "branch": "garden/test", "base": "main"}}
    monkeypatch.setattr(sched, "_dispatch_check_run", lambda *_a, **_k: (_ for _ in ()).throw(ResourcePressureError("full")))

    with pytest.raises(ResourcePressureError, match="full"):
        sched.delegate_recovery(task)
    assert st["needs_human"]["kind"] == "check_did_not_run"
    assert not st.get("delegated_recovery_fingerprints")


def test_infrastructure_and_missing_ci_are_operator_actions_not_owner_cards(sched):
    """Routine prerequisites name their repair without masquerading as product decisions."""
    from garden.inbox import build_inbox, needs_you

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["infrastructure_hold"] = {"kind": "missing_libraries", "diagnostic": "install libnss3"}
    st["ci_missing"] = True

    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    assert [card["kind"] for card in cards if card["group"] == "operator"] == ["ci_missing"]
    assert not any(needs_you(card) for card in cards if card["group"] == "operator")

    st["ci_missing"] = False
    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    assert [card["kind"] for card in cards if card["group"] == "operator"] == ["infrastructure_hold"]


def test_mixed_checkout_and_live_config_work_waits_for_operator_evidence(sched, fake_github):
    """A live setting is an operator prerequisite, never a worker instruction or owner card."""
    from garden.brief import build_brief
    from garden.inbox import build_inbox, needs_you

    task = sched.store.task("DM-001")
    task.extra["deliverables"] = [
        {"path": "src/demo.py", "action": "add the product behavior"},
        {"path": "/etc/demo/live.yaml", "owner": "operator", "action": "enable the deployed feature"},
    ]
    sched.store.save(task)

    # Preflight parks the live change as an operator action before a worker/run exists.
    assert not sched.operator_scope_ready(task)
    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    card = next(item for item in cards if item["kind"] == "operator_scope")
    assert card["group"] == "operator"
    assert not needs_you(card)
    assert "/etc/demo/live.yaml" in card["reason"]
    assert "enable the deployed feature" not in build_brief(sched.store, task).text
    assert not sched.runs.runs_for(task.id)

    sched.submit_operator_evidence(task, "deployed setting enabled in the disposable environment")
    assert sched.operator_scope_ready(sched.store.task(task.id))
    evidence = sched.state.get(task.id)["operator_evidence"]
    assert evidence["text"] == "deployed setting enabled in the disposable environment"

    # The product-only work now runs normally; the worker still receives no live config step.
    assert "DM-001(work)" in sched.tick().dispatched


# ---- CG-142: a done or cancelled task is terminal; no action reopens it -----
@pytest.mark.parametrize("action", ["triage", "retry", "cancel", "answer", "accept_decision", "reject_decision", "resume_task", "dispatch", "dispatch_review", "review_again", "dispatch_persona_pr", "integrate_now"])
def test_state_changing_actions_refuse_a_merged_done_task(sched, fake_github, action):
    """A task whose PR was merged (poll sets `done`) must not be moved back into the loop
    by any of the actions a stale page or a race could still fire."""
    sched.tick()
    sched.tick()  # DM-001 -> in_review with an open PR
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    task.log("PR merged: https://github.com/test/demo/pull/71")
    task.status = Status.DONE
    sched.store.save(task)

    kwargs = {}
    if action == "triage":
        kwargs = {"ready": True}
    elif action == "answer":
        kwargs = {"text": "hi"}
    elif action in ("accept_decision", "reject_decision"):
        kwargs = {"note": "ok"} if action == "accept_decision" else {"note": "no"}
    elif action == "dispatch_persona_pr":
        kwargs = {"name": "security"}

    with pytest.raises(RuntimeError, match=r"DM-001 is done: #71 was merged"):
        getattr(sched, action)(sched.store.task("DM-001"), **kwargs)

    assert statuses(sched)["DM-001"] == "done"  # untouched by the refused action


def test_cancel_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-175: cancelling a task with a stop recorded (a review cap, feedback waiting for a
    revise run, an automerge hold) must drop them so the cancelled task never shows as a
    decision on the Inbox."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_wont_do_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-195: `wont_do` is terminal alongside done and cancelled, so it must clear the same
    stale stops on transition — it was missing from the CG-175 fix, which only checked
    Status.DONE/CANCELLED."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.mark_wont_do(task, reason="not worth doing")
    assert statuses(sched)["DM-001"] == "wont_do"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_mark_done_requires_pr_commits_on_the_base_unless_forced(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    sched.store.save(task)
    monkeypatch.setattr(sched, "_pr_commits_on_base", lambda _: False)

    with pytest.raises(RuntimeError, match="commits are not on its base branch"):
        sched.mark_done(task)
    assert statuses(sched)["DM-001"] == "ready"

    sched.mark_done(task, force=True)
    assert statuses(sched)["DM-001"] == "done"
    done = sched.events.read(task_id=task.id, kinds=("transition",))[-1]
    assert done["base_merged"] is False


def test_forced_done_custom_note_is_not_a_merge(sched):
    task = sched.store.task("DM-001")
    sched.mark_done(task, note="obsolete", force=True)
    done = sched.events.read(task_id=task.id, kinds=("transition",))[-1]
    assert done["base_merged"] is False


def test_external_open_pr_uses_claimed_identity_and_review_without_managed_worktree(sched, fake_github):
    """An operator-owned directory is audit data, not a signal to push or test it."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    pr.head_sha = "verified-head"
    coincidental_path = sched.worktree_for(task)
    coincidental_path.mkdir(parents=True)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override="operator/fix", completion_mode="external",
                         external_pr=pr.url, worktree_override=coincidental_path)
    assert run.completion_mode == "external" and run.worktree == str(coincidental_path)

    sched.finish_manual(task, {"status": "done", "summary": "implemented", "pr": pr.url})

    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW and task.branch == "operator/fix"
    assert sched.state.get(task.id)["pr_number"] == pr.number
    assert any(r.mode == "review" for r in sched.runs.runs_for(task.id))


def test_external_claim_persists_actual_identity_before_finish(sched, fake_github):
    """A restart after take retains the operator's branch and PR, not a generated default."""
    from garden.store import Store

    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    pr.head_sha = "verified-head"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    reloaded = Store(sched.store.root).task(task.id)
    assert reloaded.branch == "operator/actual"
    assert reloaded.pr == pr.url
    assert sched.state.get(task.id)["pr_number"] == pr.number


def test_external_claim_stores_a_safe_provider_identity_without_a_browser_url(sched, fake_github):
    """Provider identities are accepted after the CLI has verified their PR number."""
    task = sched.store.task("DM-001")
    provider_url = "https://provider.test/api/pull-requests/opaque-identity"
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    pr.head_sha = "verified-head"

    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/actual", completion_mode="external",
                   external_pr=provider_url, external_pr_number=pr.number)

    assert sched.store.task(task.id).pr == provider_url
    assert sched.state.get(task.id)["pr_number"] == pr.number


@pytest.mark.parametrize("url", [
    "https://operator:synthetic-password@provider.test/pull/101",
    "https://operator@provider.test/pull/101",
    "https://provider.test/pull/101?access=synthetic-token",
    "https://provider.test/pull/101#synthetic-fragment",
])
def test_external_claim_rejects_unsafe_provider_identity_before_persistence(sched, url):
    task = sched.store.task("DM-001")

    with pytest.raises(RuntimeError, match="unsupported components"):
        sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                       branch_override="operator/actual", completion_mode="external",
                       external_pr=url, external_pr_number=101)

    assert sched.store.task(task.id).pr == ""
    assert not sched.runs.all_runs()


def test_pushed_manual_completion_fetches_exact_head_and_enters_normal_review(sched, fake_github, tmp_path):
    """Work from another clone is materialised before the ordinary PR/review handoff."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    task = sched.store.task("DM-001")
    branch = "operator/pushed"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "authoring-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "manual.txt").write_text("authored elsewhere\n")
    gitops.git("add", "manual.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "external work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)
    result = {
        "status": "done", "summary": "authored elsewhere", "repository": "test/demo",
        "branch": branch, "pushed_sha": pushed_sha,
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    }

    sched.finish_manual(task, result)

    worktree = sched.worktree_for(task)
    assert gitops.git("rev-parse", "HEAD", cwd=worktree).strip() == pushed_sha
    assert sched.store.task(task.id).status == Status.IN_REVIEW
    assert any(r.mode == "review" for r in sched.runs.runs_for(task.id))


@pytest.mark.parametrize(
    ("repository", "branch", "sha", "message"),
    [
        ("other/repo", "operator/pushed", "a" * 40, "does not match configured repository"),
        ("test/demo", "operator/other", "a" * 40, "does not match claimed branch"),
        ("test/demo", "operator/pushed", "short", "exact full commit SHA"),
        ("test/demo", "operator/pushed", "a" * 40, "was not found"),
    ],
)
def test_pushed_manual_completion_refuses_untrusted_identity(
    sched, fake_github, repository, branch, sha, message,
):
    task = sched.store.task("DM-001")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/pushed", completion_mode="pushed")

    with pytest.raises(RuntimeError, match=message):
        sched.finish_manual(task, {"status": "done", "repository": repository,
                                   "branch": branch, "pushed_sha": sha})

    saved = sched.runs.latest(task.id)
    assert saved.status == "running"
    assert saved.completion_attempts[-1]["status"] == "refused"
    assert sched.store.task(task.id).status == Status.RUNNING


def test_pushed_manual_stale_sha_can_be_corrected_after_restart(sched, fake_github, tmp_path):
    task = sched.store.task("DM-001")
    branch = "operator/recover"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "recovery-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "recovery.txt").write_text("recoverable\n")
    gitops.git("add", "recovery.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "recoverable work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)

    with pytest.raises(RuntimeError, match="SHA is stale"):
        sched.finish_manual(task, {"status": "done", "repository": "test/demo",
                                   "branch": branch, "pushed_sha": "a" * 40})

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    restarted.finish_manual(restarted.store.task(task.id), {
        "status": "done", "repository": "test/demo", "branch": branch,
        "pushed_sha": pushed_sha, "summary": "recovered",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    })
    assert restarted.store.task(task.id).status == Status.IN_REVIEW
    saved = restarted.runs.latest(task.id)
    assert saved.pushed_head == pushed_sha
    assert saved.completion_attempts[-1]["status"] == "refused"


def test_pushed_manual_submitted_result_resumes_finalization_after_restart(sched, fake_github, tmp_path):
    task = sched.store.task("DM-001")
    branch = "operator/interrupted"
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "interrupted-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "interrupted.txt").write_text("durable\n")
    gitops.git("add", "interrupted.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "durable work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)
    result = {
        "status": "done", "repository": "test/demo", "branch": branch,
        "pushed_sha": pushed_sha, "summary": "interrupted",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    }
    run.pushed_head = pushed_sha
    ManualRunner.finish(run, result)
    run.env_snapshot["pushed_completion_submitted"] = True
    run.finished_at = now_iso()
    run.status = "done"
    run.save()  # controller stops during finalize(), before the task transition

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    restarted.tick()

    assert restarted.store.task(task.id).status == Status.IN_REVIEW
    assert gitops.git("rev-parse", "HEAD", cwd=restarted.worktree_for(task)).strip() == pushed_sha


def test_external_claim_refuses_pr_with_a_different_actual_branch(sched, fake_github):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="garden/DM-001-generated", completion_mode="external")

    with pytest.raises(RuntimeError, match="does not match claimed branch"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    assert sched.store.task(task.id).status == Status.RUNNING
    failed = sched.runs.latest(task.id)
    assert failed.status == "running"
    assert failed.completion_attempts[-1]["status"] == "refused"
    assert failed.completion_attempts[-1]["cost_usd"] is None
    assert failed.completion_attempts[-1]["pr_url"] == pr.url
    assert failed.completion_attempts[-1]["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number


@pytest.mark.parametrize("error_type", [GitHubError, KeyError])
def test_external_claim_refuses_inaccessible_pr_metadata_without_creating_run(
    sched, fake_github, monkeypatch, error_type,
):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")

    def unavailable(*_):
        raise error_type("unavailable")

    monkeypatch.setattr(sched.github, "get_pr", unavailable)

    with pytest.raises(RuntimeError, match="could not read external PR: unavailable"):
        sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                       branch_override=pr.head, completion_mode="external",
                       external_pr=pr.url)

    saved = sched.store.task(task.id)
    assert saved.branch == ""
    assert saved.pr == ""
    assert saved.status == Status.READY
    assert not sched.runs.runs_for(task.id)


@pytest.mark.parametrize("missing", ["head_sha", "base"])
def test_external_claim_refuses_incomplete_pr_metadata_without_creating_run(
    sched, fake_github, missing,
):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    setattr(pr, missing, "")

    with pytest.raises(RuntimeError, match="missing immutable head or base metadata"):
        sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                       branch_override=pr.head, completion_mode="external",
                       external_pr=pr.url)

    assert sched.store.task(task.id).pr == ""
    assert not sched.runs.runs_for(task.id)


@pytest.mark.parametrize("error_type", [GitHubError, KeyError])
def test_external_completion_pr_lookup_failure_is_audited(sched, fake_github, monkeypatch, error_type):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    pr.head_sha = "verified-head"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    def unavailable(*_):
        raise error_type("unavailable")

    monkeypatch.setattr(sched.github, "get_pr", unavailable)

    with pytest.raises(RuntimeError, match="could not read external PR: .*unavailable"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.RUNNING
    assert sched.store.task(task.id).pr == pr.url
    refused = sched.runs.latest(task.id).completion_attempts[-1]
    assert refused["pr_url"] == pr.url and refused["pr_number"] == pr.number
    assert refused["cost_usd"] is None
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number


def test_external_blocked_result_uses_ordinary_manual_completion(sched, fake_github):
    task = sched.store.task("DM-001")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/blocked", completion_mode="external")

    rep = sched.finish_manual(task, {"status": "blocked", "summary": "waiting on access"})

    assert sched.store.task(task.id).status == Status.FAILED
    assert "failed" in rep.transitions[0]
    assert sched.runs.latest(task.id).result["status"] == "blocked"


def test_external_merged_pr_completes_without_rechecks_after_final_base_verification(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/merged", "main", "external", "")
    pr.state, pr.head_sha, pr.merge_commit_sha = "MERGED", "verified-head", "verified-merge"
    monkeypatch.setattr(gitops, "fetch", lambda _: True)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: True)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    rep = sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.DONE
    assert "external merged PR" in rep.transitions[0]
    assert sched.runs.latest(task.id).run_id == run.run_id
    assert not any(r.mode == "review" for r in sched.runs.runs_for(task.id))
    with pytest.raises(RuntimeError, match="no active run to finish"):
        sched.finish_manual(sched.store.task(task.id), {"status": "done", "pr": pr.url})


def _merged_external_topology(sched, fake_github, method: str, *, mismatch: bool = False):
    """Build the same source change with merge, squash, or rebased commits on main."""
    repo = sched.repo_for(sched.store.task("DM-001"))
    base = gitops.git("rev-parse", "main", cwd=repo).strip()
    branch = f"operator/{method}"
    gitops.git("checkout", "-q", "-b", branch, base, cwd=repo)
    (repo / "one.txt").write_text("one\n")
    gitops.git("add", "one.txt", cwd=repo)
    gitops.git("commit", "-q", "-m", "one", cwd=repo)
    (repo / "two.txt").write_text("two\n")
    gitops.git("add", "two.txt", cwd=repo)
    gitops.git("commit", "-q", "-m", "two", cwd=repo)
    head = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    source_commits = gitops.git("rev-list", "--reverse", f"{base}..{head}", cwd=repo).split()
    gitops.git("checkout", "-q", "main", cwd=repo)
    if method == "merge":
        gitops.git("merge", "-q", "--no-ff", "-m", "merge", head, cwd=repo)
    elif method == "squash":
        gitops.git("merge", "-q", "--squash", head, cwd=repo)
        gitops.git("commit", "-q", "-m", "squash", cwd=repo)
    else:
        for commit in source_commits:
            gitops.git("cherry-pick", commit, cwd=repo)
    if mismatch:
        (repo / "two.txt").write_text("different\n")
        gitops.git("add", "two.txt", cwd=repo)
        gitops.git("commit", "-q", "--amend", "--no-edit", cwd=repo)
    merge_commit = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    gitops.git("push", "-q", "origin", "main", cwd=repo)
    pr = fake_github.create_pr("test/demo", branch, "main", "external", "")
    pr.state, pr.head_sha, pr.merge_commit_sha = "MERGED", head, merge_commit
    return pr, head, merge_commit


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_external_merged_pr_accepts_verified_git_topologies(sched, fake_github, method):
    task = sched.store.task("DM-001")
    pr, head, merge_commit = _merged_external_topology(sched, fake_github, method)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.DONE
    state = sched.state.get(task.id)
    assert state["head_sha"] == head
    assert state["merge_commit_sha"] == merge_commit
    assert not state.get("automerged")
    assert sched.runs.latest(task.id).run_id == run.run_id


def test_external_squash_rejects_partial_or_different_content(sched, fake_github):
    task = sched.store.task("DM-001")
    pr, _, _ = _merged_external_topology(sched, fake_github, "squash", mismatch=True)
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    with pytest.raises(RuntimeError, match="does not match the result"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    assert sched.store.task(task.id).status == Status.RUNNING


def test_external_completion_rejects_coincidental_merge_commit(sched, fake_github):
    """Source and merge commit on final base are insufficient when unrelated."""
    task = sched.store.task("DM-001")
    pr, head, _ = _merged_external_topology(sched, fake_github, "merge")
    repo = sched.repo_for(task)
    gitops.git("checkout", "-q", "-b", "unrelated-result", f"{head}~2", cwd=repo)
    (repo / "coincidental.txt").write_text("unrelated\n")
    gitops.git("add", "coincidental.txt", cwd=repo)
    gitops.git("commit", "-q", "-m", "unrelated merge result", cwd=repo)
    pr.merge_commit_sha = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    gitops.git("checkout", "-q", "main", cwd=repo)
    gitops.git("merge", "-q", "--no-ff", "-m", "include unrelated result",
               pr.merge_commit_sha, cwd=repo)
    gitops.git("push", "-q", "origin", "main", cwd=repo)
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    with pytest.raises(RuntimeError, match="does not match the result"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.RUNNING


def test_external_merged_pr_retains_refusal_before_verified_retry(sched, fake_github):
    task = sched.store.task("DM-001")
    pr, head, merge_commit = _merged_external_topology(sched, fake_github, "squash")
    pr.merge_commit_sha = ""
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    with pytest.raises(RuntimeError, match="missing immutable"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    pr.head_sha, pr.merge_commit_sha = head, merge_commit

    sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.runs.latest(task.id).completion_attempts[-1]["status"] == "refused"
    assert sched.runs.latest(task.id).result["status"] == "done"


def test_external_claim_rejects_a_head_that_moves_before_completion(sched, fake_github):
    task = sched.store.task("DM-001")
    pr, _, _ = _merged_external_topology(sched, fake_github, "squash")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    pr.head_sha = "f" * 40

    with pytest.raises(RuntimeError, match="head moved since it was claimed"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})


def test_external_completion_rejects_same_pr_number_from_another_repository(sched, fake_github):
    task = sched.store.task("DM-001")
    pr, _, _ = _merged_external_topology(sched, fake_github, "squash")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    wrong_url = f"https://wrong.example/other/repository/pull/{pr.number}"

    with pytest.raises(RuntimeError, match="does not match the provider identity"):
        sched.finish_manual(task, {"status": "done", "pr": wrong_url})


def test_external_merged_pr_restacks_its_child(sched, fake_github, monkeypatch):
    """An external parent merge shares the normal stacked-child lifecycle."""
    parent = sched.store.task("DM-001")
    child = sched.store.task("DM-002")
    pr = fake_github.create_pr("test/demo", "operator/merged", "main", "external", "")
    pr.state, pr.head_sha, pr.merge_commit_sha = "MERGED", "verified-head", "verified-merge"
    sched.state.get(child.id)["stack_parent"] = parent.id
    restacked: list[str] = []
    monkeypatch.setattr(gitops, "fetch", lambda _: True)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: True)
    monkeypatch.setattr(sched, "_restack", lambda task, _: restacked.append(task.id))
    sched.dispatch(parent, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    sched.finish_manual(parent, {"status": "done", "pr": pr.url})

    assert restacked == [child.id]


def test_external_stacked_merged_pr_is_not_completed_until_it_reaches_final_base(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/stacked", "parent-branch", "external", "")
    pr.state, pr.head_sha, pr.merge_commit_sha = "MERGED", "stacked-head", "stacked-merge"
    monkeypatch.setattr(gitops, "fetch", lambda _: True)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: False)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    with pytest.raises(RuntimeError, match="does not match configured final base"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    assert sched.store.task(task.id).status == Status.RUNNING
    failed = sched.runs.latest(task.id)
    assert failed.run_id == run.run_id and failed.status == "running"
    assert "does not match configured final base" in failed.completion_attempts[-1]["reason"]
    assert failed.completion_attempts[-1]["pr_url"] == pr.url
    assert failed.completion_attempts[-1]["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number


def test_external_completion_git_guard_violation_is_refused_and_failed(sched, fake_github):
    """External completion must not skip the metadata guard captured at dispatch."""
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    pr.head_sha = "verified-head"
    created_before = len(fake_github.created)
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    clone = sched.repo_for(task)
    config = clone / ".git" / "config"
    config.write_text(config.read_text() + "\n[core]\n\thooksPath = /tmp/garden-test-evil-hooks\n")

    rep = sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.FAILED
    assert "git guard" in rep.transitions[0]
    refused = sched.runs.latest(task.id).completion_attempts[-1]
    assert refused["status"] == "refused"
    assert refused["pr_url"] == pr.url and refused["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number
    assert len(fake_github.created) == created_before
    with pytest.raises(gitops.GitError):
        gitops.git("status", cwd=clone)


def test_tick_sweeps_stale_state_off_a_task_already_terminal(sched, fake_github):
    """CG-195: a task that reached done/cancelled/wont_do before `_transition` cleared these
    fields (or through a path that bypassed it, e.g. a hand-edited state.json) must not keep
    showing a decision forever — a plain tick sweeps every terminal task's stale needs_human,
    pending feedback and automerge stop, not just the moment of transition."""
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.tick()
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


@pytest.mark.parametrize("terminal", [Status.DONE, Status.CANCELLED, Status.WONT_DO])
def test_terminal_task_retires_collected_check_without_losing_evidence(sched, terminal):
    """CG-386: a stale collected continuation cannot reopen a merged or otherwise closed task."""
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.cost_usd = 1.25
    run.result = {"checks": []}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        "collected": True,
    }
    sched.state.save()

    sched._transition(task, terminal, "terminal lifecycle regression")
    assert not sched.state.get(task.id).get("check_run")
    assert sched.reap_check(sched.store.task(task.id), type("Report", (), {})()) is False
    assert sched.store.task(task.id).status == terminal
    preserved = sched._run_by_id(task, run.run_id)
    assert preserved is not None
    assert preserved.result == {"checks": []}
    assert preserved.cost_usd == 1.25


def test_check_collection_discards_legacy_continuation_for_terminal_task(sched):
    """A task made terminal outside `_transition` is still protected at collection time."""
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": []}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        "collected": True,
    }
    task.status = Status.DONE
    sched.store.save(task)
    sched.state.save()

    assert sched.reap_check(sched.store.task(task.id), type("Report", (), {})()) is True
    assert not sched.state.get(task.id).get("check_run")
    assert sched.store.task(task.id).status == Status.DONE
    assert not sched.state.get(task.id).get("needs_human")


def test_terminal_task_keeps_check_ownership_when_run_record_is_missing(sched):
    """Missing metadata cannot prove that the detached process behind it has stopped."""
    task = sched.store.task("DM-001")
    pointer = {"run_id": "missing-check-run", "stage": "base_probe", "collected": True}
    sched.state.get(task.id)["check_run"] = pointer
    sched.state.save()

    sched._transition(task, Status.DONE, "terminal with incomplete run history")

    assert sched.store.task(task.id).status == Status.DONE
    assert sched.state.get(task.id)["check_run"] == pointer


def test_terminal_task_stops_live_check_before_releasing_ownership(sched):
    """A real detached child is confirmed dead before its continuation is retired."""
    task = sched.store.task("DM-001")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        run = sched.runs.new_run(task.id, "local", mode="check")
        run.pid = child.pid
        run.save()
        sched.state.get(task.id)["check_run"] = {
            "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        }
        sched.state.save()

        sched._transition(task, Status.CANCELLED, "terminal while check is live")

        child.wait(timeout=10)
        assert child.poll() is not None
        assert not sched.state.get(task.id).get("check_run")
        retired = sched._run_by_id(task, run.run_id)
        assert retired is not None
        assert retired.status == "cancelled"
        assert retired.error == "task reached terminal status"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_cancel_refuses_an_already_cancelled_task(sched):
    task = sched.store.task("DM-001")
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    with pytest.raises(RuntimeError, match=r"DM-001 is cancelled: cancelled by hand"):
        sched.retry(sched.store.task("DM-001"))
    assert statuses(sched)["DM-001"] == "cancelled"  # still cancelled, not reopened


# ---- approve refuses an incomplete brief (CG-193) ----


def _draft(sched, body, reading=None):
    t = sched.store.create_task("demo", "p1", "Needs a real brief", body,
                                reading=reading or [], status="draft")
    return sched.store.task(t.id)


def test_approve_refuses_placeholder_criteria(sched, fake_github):
    t = _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="incomplete brief"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_refuses_unresolved_reading_path(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/nope.md"])
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="reading-list path not found"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_accepts_a_complete_brief(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/spec.md"])
    ph = sched.store.phase("demo", "p1")
    sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.READY


def test_inbox_approve_card_shows_the_gap(sched, fake_github):
    from garden.inbox import build_inbox

    _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    items = build_inbox(sched.store, sched)
    card = next(i for i in items if i["group"] == "approve" and i["title"] == "Needs a real brief")
    assert card["gaps"]
    assert "brief incomplete" in card["why"]
