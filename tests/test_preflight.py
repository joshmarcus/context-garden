from __future__ import annotations

from garden.preflight import PREFLIGHT_ITEMS, mechanical_results, missing_preflight
from garden.review import review_brief
from garden.store import Store


def test_brief_requires_a_complete_preflight(garden):
    from garden.brief import build_brief

    text = build_brief(Store(garden), Store(garden).task("DM-001")).text
    assert "## Review pre-flight" in text
    assert "pre_flight" in text
    assert missing_preflight([]) == list(PREFLIGHT_ITEMS)
    rows = [{"item": item, "status": "pass"} for item in PREFLIGHT_ITEMS]
    assert missing_preflight(rows) == []


def test_mechanical_preflight_checks_each_failure_shape(garden, monkeypatch):
    worktree = garden / "work"
    worktree.mkdir()
    (worktree / "bad.py").write_text("def broken(:\n")
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: (
        "+<<<<<<< ours\n+=======\n+>>>>>>> theirs\n" if "--name-only" not in args else "bad.py\nsrc/garden/web/page.html\n"
    ))
    results = mechanical_results(worktree, "main", "", require_description=True, ui_changed=True, captures=[])
    failed = {row["name"] for row in results if row["status"] == "fail"}
    assert failed == {"conflict markers", "syntax", "UI captures", "PR description"}


def test_mechanical_preflight_checks_pass_a_clean_diff(garden, monkeypatch):
    worktree = garden / "clean"
    worktree.mkdir()
    (worktree / "good.py").write_text("VALUE = 1\n")
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: "+VALUE = 1\n" if "--name-only" not in args else "good.py\n")
    results = mechanical_results(worktree, "main", "A useful description", require_description=True,
                                ui_changed=False, captures=[])
    assert {row["status"] for row in results} == {"pass"}


def test_mechanical_preflight_ignores_deleted_python_modules(garden, monkeypatch):
    worktree = garden / "deleted"
    worktree.mkdir()
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: "-def old(:\n" if "--name-only" not in args else "removed.py\n")
    results = mechanical_results(worktree, "main", "Description", require_description=True,
                                ui_changed=False, captures=[])
    assert {row["status"] for row in results} == {"pass"}


def test_mechanical_preflight_fails_closed_when_git_inspection_fails(garden, monkeypatch):
    worktree = garden / "uninspectable"
    worktree.mkdir()
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    calls = 0

    def inspect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise gitops.GitError("could not list changed paths")
        return "+VALUE = 1\n"

    monkeypatch.setattr(gitops, "git", inspect)

    results = mechanical_results(worktree, "main", "Description", require_description=True,
                                ui_changed=False, captures=[])

    assert results == [{"name": "mechanical pre-flight", "status": "fail",
                        "summary": "could not inspect candidate diff: could not list changed paths", "details": ""}]


def test_mechanical_preflight_syntax_check_does_not_write_bytecode(garden, monkeypatch):
    worktree = garden / "no-bytecode"
    worktree.mkdir()
    (worktree / "good.py").write_text("VALUE = 1\n")
    before = {path.relative_to(worktree) for path in worktree.rglob("*")}
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: "+VALUE = 1\n" if "--name-only" not in args else "good.py\n")

    results = mechanical_results(worktree, "main", "Description", require_description=True,
                                ui_changed=False, captures=[])

    assert {row["status"] for row in results} == {"pass"}
    assert {path.relative_to(worktree) for path in worktree.rglob("*")} == before


def test_mechanical_preflight_finds_ui_changes_from_inspected_paths(garden, monkeypatch):
    worktree = garden / "ui-change"
    worktree.mkdir()
    from garden import gitops

    monkeypatch.setattr(gitops, "base_ref", lambda *_args: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: "+body {}\n" if "--name-only" not in args else "static/site.css\n")

    results = mechanical_results(worktree, "main", "Description", require_description=True,
                                ui_changed=False, captures=[])

    assert next(row for row in results if row["name"] == "UI captures")["status"] == "fail"


def test_review_brief_uses_frozen_criteria_and_marks_delta(garden):
    store = Store(garden)
    task = store.task("DM-001")
    frozen = ["The original criterion is met."]
    task.body += "\n## Acceptance criteria\n\n- [ ] The later criterion is met.\n"
    text = review_brief(store, task, branch="b", base="main", pr_title="T", pr_body="B", diff="",
                        max_diff_chars=1000, criteria_snapshot=frozen,
                        verified=[{"criterion": frozen[0], "evidence": "test_original"}])
    assert "Criteria frozen for this dispatch" in text
    assert "## Review pre-flight" in text
    assert "Lint is clean" in text
    assert frozen[0] in text
    assert "## Criteria changed after dispatch" in text
    assert "The later criterion is met." in text


def test_criteria_edit_after_dispatch_is_a_note_in_the_revise_brief(sched, monkeypatch):
    """The first review is against the worker's contract; the edit reaches revise."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")

    task = sched.store.task("DM-001")
    task.body += "\n## Acceptance criteria\n\n- [ ] The original criterion is met.\n"
    sched.store.save(task)
    sched.tick()  # dispatch work with the original criteria
    task = sched.store.task("DM-001")
    task.body = task.body.replace("The original criterion is met.", "The later criterion is met.")
    sched.store.save(task)
    sched.store.invalidate()  # emulate the next scheduler tick seeing the task-file edit

    sched.tick()  # reap work, open PR, and dispatch review
    sched.tick()  # reap review and dispatch revise
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "### Criteria changed after dispatch" in brief
    assert "Added: The later criterion is met." in brief
    assert "Removed: The original criterion is met." in brief


def test_criteria_edit_reaches_revise_after_a_mechanical_pre_pr_failure(sched, monkeypatch):
    """A result-gate failure still carries edits made after the worker's dispatch."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "omit-preflight")
    task = sched.store.task("DM-001")
    task.body += "\n## Acceptance criteria\n\n- [ ] The original criterion is met.\n"
    sched.store.save(task)

    sched.tick()  # dispatch work with the original criterion
    task = sched.store.task("DM-001")
    task.body = task.body.replace("The original criterion is met.", "The later criterion is met.")
    sched.store.save(task)
    sched.store.invalidate()

    sched.tick()  # reap the incomplete result and dispatch the automatic revise
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "pre-PR check" in brief
    assert "### Criteria changed after dispatch" in brief
    assert "Added: The later criterion is met." in brief
    assert "Removed: The original criterion is met." in brief


def test_empty_criteria_snapshot_does_not_adopt_later_task_edits(sched):
    task = sched.store.task("DM-001")
    review_run = sched.runs.new_run(task.id, "local", mode="review")
    review_run.env_snapshot = {"criteria": []}
    task.body += "\n## Acceptance criteria\n\n- [ ] A later criterion.\n"

    assert "Added: A later criterion." in sched._criteria_changed_note(task, review_run)


def test_static_assets_are_ui_changes():
    from garden.scheduler.checkruns import _is_ui_path

    assert _is_ui_path("static/site.js")


def test_requeued_review_keeps_the_author_preflight(sched):
    sched.tick()
    sched.tick()  # reap the worker result; the pre-PR check may still be in flight
    task = sched.store.task("DM-001")

    review_run = sched.dispatch_review(task)

    text = (review_run.path / "brief.md").read_text()
    assert "## Author's pre-flight" in text
    assert "A test or stated reason for every acceptance criterion" in text


def test_missing_preflight_is_sent_back_before_a_pr(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "omit-preflight")
    sched.tick()
    report = sched.tick()
    task = sched.store.task("DM-001")
    assert task.status.value == "running"  # the automatic revise starts in the same tick
    assert not task.pr
    first = sched.runs.runs_for(task.id)[0]
    assert "missing review pre-flight" in first.error
    assert "DM-001 -> changes_requested (checks)" in report.transitions


def test_scheduler_turns_changed_path_inspection_failure_into_revise_feedback(sched, monkeypatch):
    """A Git error while scheduling a pre-PR check must not abort the scheduler tick."""
    from garden import gitops

    sched.cfg.data["checks"] = {"pre_pr": [{"name": "noop", "command": "true"}]}
    monkeypatch.setattr(gitops, "diff_names", lambda *_args: (_ for _ in ()).throw(gitops.GitError("cannot inspect paths")))

    sched.tick()  # dispatch worker
    sched.tick()  # reap worker and start pre-PR check despite the inspection error
    report = sched.tick()  # reap check and start the automatic revise

    task = sched.store.task("DM-001")
    assert task.status.value == "running"
    assert "DM-001 -> changes_requested (checks)" in report.transitions
    revise = sched.runs.latest(task.id)
    assert revise.mode == "revise"
    assert "could not inspect candidate diff: cannot inspect paths" in (revise.path / "brief.md").read_text()
