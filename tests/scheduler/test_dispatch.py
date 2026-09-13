"""Dispatch: the queue, slots and the live max_parallel override, the pause, and the stuck-task audit."""

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Status, parse_execution_requirements
from garden.scheduler import StateCorruptionError
from garden.scheduler.dispatch import MAX_SERIALIZED_PROMPT_BYTES
from garden.scheduler.report import TickReport
from garden.scheduler.resources import ResourcePressureError
from garden.suggestions import record_suggestion
from tests.scheduler.conftest import statuses


def test_tick_refuses_to_schedule_from_corrupt_state(sched):
    corrupt = b'{"_control":{"dispatch":"paused"}'
    sched.state.path.write_bytes(corrupt)
    runs_before = list(sched.runs.active())

    with pytest.raises(StateCorruptionError, match="state is corrupt"):
        sched.tick()

    assert sched.state.path.read_bytes() == corrupt
    assert list(sched.runs.active()) == runs_before


def test_constrained_task_without_same_user_match_consumes_no_attempt(sched):
    task = sched.store.task("DM-001")
    task.owner = "alice"
    task.execution_requirements = parse_execution_requirements({
        "capabilities": {"all_of": ["tool.build"]},
    })
    sched.store.save(task)
    sched.cfg.data["capability_definitions"] = {
        "tool.build": {"type": "tool", "description": "builder",
                       "issuer": "operator", "privileged": False},
    }

    report = sched.tick()

    current = sched.store.task(task.id)
    assert current.status is Status.READY
    assert current.attempts == 0
    assert sched.runs.latest(task.id) is None
    assert any("no_compatible_profile" in error for error in report.errors)


def test_manual_take_cannot_bypass_constrained_worker_claim(sched):
    task = sched.store.task("DM-001")
    task.owner = "alice"
    task.runner = "manual"
    task.execution_requirements = parse_execution_requirements({
        "capabilities": {"all_of": ["tool.build"]},
    })
    sched.store.save(task)
    sched.cfg.data.update({
        "capability_definitions": {
            "tool.build": {"type": "tool", "description": "builder",
                           "issuer": "operator", "privileged": False},
        },
        "worker_configurations": {"builder": {
            "contract_version": "garden.worker-configuration/v1", "version": "1",
            "generation": 1, "activities": ["work"], "projects": ["demo"],
            "grants": [{"capability": "tool.build", "approved_by": "operator",
                        "approved_at": 1, "profile_generation": 1}],
        }},
        "worker_instances": [{
            "instance_id": "build-1", "configuration": "builder",
            "configuration_version": "1", "profile_generation": 1,
            "operating_user": "alice", "installation_id": "install-a",
            "authenticated_at": 1, "readiness_checked_at": 1,
            "readiness_expires_at": 4_102_444_800,
        }],
    })

    with pytest.raises(RuntimeError, match="authenticated pull-worker claim"):
        sched.take_manual(task)

    assert sched.store.task(task.id).attempts == 0
    assert sched.runs.latest(task.id) is None


def _configure_capability_worker(sched, task, *, activities):
    task.owner = "alice"
    task.execution_requirements = parse_execution_requirements({
        "capabilities": {"all_of": ["tool.build"]},
        "resources": {"memory_mib": 1024},
    })
    sched.store.save(task)
    sched.cfg.data.update({
        "capability_definitions": {
            "tool.build": {"type": "tool", "description": "builder",
                           "issuer": "operator", "privileged": False},
        },
        "worker_configurations": {"builder": {
            "contract_version": "garden.worker-configuration/v1", "version": "1",
            "generation": 1, "activities": activities, "projects": ["demo"],
            "resource_ceilings": {"memory_mib": 2048},
            "grants": [{"capability": "tool.build", "approved_by": "operator",
                        "approved_at": 1, "profile_generation": 1}],
        }},
        "worker_instances": [{
            "instance_id": "build-1", "configuration": "builder",
            "configuration_version": "1", "profile_generation": 1,
            "operating_user": "alice", "installation_id": "install-a",
            "authenticated_at": 1, "readiness_checked_at": 1,
            "readiness_expires_at": 4_102_444_800,
        }],
    })


def test_execution_envelope_records_activity_owner_requirements_and_claim_fence(sched):
    task = sched.store.task("DM-001")
    _configure_capability_worker(sched, task, activities=["review"])
    requirements, match = sched._execution_match(task, "review")
    run = sched.runs.new_run(task.id, "remote", mode="review")

    sched._record_execution_envelope(task, run, "review", requirements, match)

    envelope = run.env_snapshot["execution_envelope"]
    assert envelope["version"] == "garden.execution-envelope/v1"
    assert envelope["activity"] == "review"
    assert envelope["owner"] == "alice"
    assert envelope["worker_instance"] == "build-1"
    assert run.env_snapshot["execution_requirements"] == task.execution_requirements.to_dict()


def test_execution_envelope_records_explicit_empty_requirements(sched):
    task = sched.store.task("DM-001")
    requirements, match = sched._execution_match(task, "work")
    run = sched.runs.new_run(task.id, "local", mode="work")

    sched._record_execution_envelope(task, run, "work", requirements, match)

    assert run.env_snapshot["execution_requirements"] == {}
    assert run.env_snapshot["execution_envelope"]["requirements_sha256"]
    assert run.env_snapshot["execution_envelope"]["worker_instance"] == ""


@pytest.mark.parametrize("mode", ["revise", "check", "review"])
def test_continuation_checkpoints_first_added_requirement_before_launch(sched, mode):
    task = sched.store.task("DM-001")
    source = sched.runs.new_run(task.id, "local", mode="work")
    source.status = "done"
    source.env_snapshot["execution_requirements"] = {}
    source.save()
    _configure_capability_worker(sched, task, activities=["work", "check", "review"])

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched._execution_match(task, mode, source_run=source)

    continuation = sched.runs.latest(task.id)
    assert continuation is not source
    assert continuation.mode == mode
    assert continuation.status == "failed"
    assert continuation.pid is None
    assert continuation.env_snapshot["execution_envelope"]["source_run_id"] == source.run_id
    assert source.status == "done"

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched._execution_match(task, mode, source_run=source)
    assert len(sched.runs.runs_for(task.id)) == 2


def test_legacy_source_cannot_authorize_new_constrained_continuation(sched):
    task = sched.store.task("DM-001")
    source = sched.runs.new_run(task.id, "local", mode="work")
    source.status = "done"
    source.save()
    _configure_capability_worker(sched, task, activities=["review"])

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched._execution_match(task, "review", source_run=source)

    assert sched.runs.latest(task.id).status == "failed"
    assert source.status == "done"


def test_legacy_unconstrained_source_can_continue_unconstrained(sched):
    task = sched.store.task("DM-001")
    source = sched.runs.new_run(task.id, "local", mode="work")
    source.status = "done"
    source.save()

    requirements, match = sched._execution_match(task, "review", source_run=source)

    assert requirements.empty
    assert match is None
    assert sched.runs.latest(task.id).run_id == source.run_id
    assert len(sched.runs.runs_for(task.id)) == 1


def test_revision_entrypoint_checkpoints_first_added_requirement(sched):
    task = sched.store.task("DM-001")
    source = sched.runs.new_run(task.id, "local", mode="work")
    source.status = "done"
    source.env_snapshot["execution_requirements"] = {}
    source.save()
    _configure_capability_worker(sched, task, activities=["work"])

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched.dispatch(task, mode="revise")

    continuation = sched.runs.latest(task.id)
    assert continuation.status == "failed"
    assert continuation.pid is None
    assert continuation.env_snapshot["execution_envelope"]["source_run_id"] == source.run_id
    assert source.status == "done"


def test_revision_entrypoint_checkpoints_changed_unsupported_requirement(sched):
    task = sched.store.task("DM-001")
    _configure_capability_worker(sched, task, activities=["work", "review"])
    source = sched.runs.new_run(task.id, "remote", mode="work")
    source.status = "done"
    source.env_snapshot.update({
        "execution_requirements": task.execution_requirements.to_dict(),
        "execution_owner": "alice",
        "worker_instance": "build-1",
        "execution_envelope": {"owner": "alice", "worker_instance": "build-1"},
    })
    source.save()
    task.execution_requirements = parse_execution_requirements({
        "capabilities": {"all_of": ["tool.build"]},
        "resources": {"memory_mib": 4096},
    })
    sched.store.save(task)

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched.dispatch(task, mode="revise")

    continuation = sched.runs.latest(task.id)
    assert continuation is not None
    assert continuation is not source
    assert continuation.mode == "revise"
    assert continuation.status == "failed"
    assert "continuation fenced before launch" in continuation.error
    assert continuation.pid is None
    assert continuation.env_snapshot["execution_envelope"]["source_run_id"] == source.run_id
    assert continuation.env_snapshot["execution_envelope"]["worker_instance"] == "build-1"
    assert continuation.env_snapshot["execution_requirements"] == task.execution_requirements.to_dict()
    assert source.status == "done"
    assert len(sched.runs.runs_for(task.id)) == 2

    with pytest.raises(ResourcePressureError, match="requirements changed.*fenced recovery"):
        sched.dispatch(task, mode="revise")
    assert len(sched.runs.runs_for(task.id)) == 2


def test_continuation_stays_on_source_worker_when_an_equivalent_worker_is_idle(sched):
    task = sched.store.task("DM-001")
    _configure_capability_worker(sched, task, activities=["work", "review"])
    second = dict(sched.cfg.data["worker_instances"][0])
    second.update({"instance_id": "build-2", "installation_id": "install-b"})
    sched.cfg.data["worker_instances"].append(second)
    source = sched.runs.new_run(task.id, "remote", mode="work")
    source.status = "done"
    source.env_snapshot.update({
        "execution_requirements": task.execution_requirements.to_dict(),
        "execution_owner": "alice",
        "worker_instance": "build-2",
        "execution_envelope": {"owner": "alice", "worker_instance": "build-2"},
    })
    source.save()

    _requirements, match = sched._execution_match(task, "review", source_run=source)

    assert match.instance.instance_id == "build-2"


def test_continuation_does_not_fallback_when_source_worker_is_unavailable(sched):
    task = sched.store.task("DM-001")
    _configure_capability_worker(sched, task, activities=["work", "review"])
    source = sched.runs.new_run(task.id, "remote", mode="work")
    source.status = "done"
    source.env_snapshot.update({
        "execution_requirements": task.execution_requirements.to_dict(),
        "execution_owner": "alice",
        "worker_instance": "retired-builder",
        "execution_envelope": {"owner": "alice", "worker_instance": "retired-builder"},
    })
    source.save()

    with pytest.raises(ResourcePressureError, match="pinned worker instance is not configured"):
        sched._execution_match(task, "review", source_run=source)


def test_continuation_rejects_owner_handoff(sched):
    task = sched.store.task("DM-001")
    _configure_capability_worker(sched, task, activities=["work", "review"])
    source = sched.runs.new_run(task.id, "remote", mode="work")
    source.status = "done"
    source.env_snapshot.update({
        "execution_requirements": task.execution_requirements.to_dict(),
        "execution_owner": "alice",
        "worker_instance": "build-1",
        "execution_envelope": {"owner": "alice", "worker_instance": "build-1"},
    })
    source.save()
    task.owner = "bob"
    sched.store.save(task)

    with pytest.raises(ResourcePressureError, match="source activity belongs.*current owner"):
        sched._execution_match(task, "review", source_run=source)

    assert source.status == "done"
    assert len(sched.runs.runs_for(task.id)) == 1


def test_duplicate_task_id_quarantined_the_tick_survives_and_dispatch_continues(sched, garden, fake_github):
    """CG-244: two files claiming one id used to make store.tasks() raise, which took down every
    page and tick. Now the ambiguous id is quarantined out of dispatch, the tick runs to
    completion, unrelated ready tasks still dispatch, and the collision is surfaced on the tick
    report and by `garden validate`."""
    for name in ("clash-a", "clash-b"):
        (garden / "demo" / "p1" / "tasks" / f"DM-050-{name}.md").write_text(textwrap.dedent(f"""
            ---
            id: DM-050
            title: {name}
            status: ready
            depends_on: []
            priority: 1
            reading: []
            created: '2026-01-01T00:00:00+00:00'
            updated: '2026-01-01T00:00:00+00:00'
            ---

            ## Goal

            {name}
            """).lstrip())
    sched.cfg.data["stack"] = False
    rep = sched.tick()  # does not raise
    assert rep.dispatched == ["DM-001(work)"]  # the healthy ready task still dispatches
    assert "DM-050" not in statuses(sched)  # the ambiguous id never entered the task map
    assert any("duplicate task id DM-050" in e for e in rep.errors)

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        result = CliRunner().invoke(app, ["validate"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "duplicate task id DM-050" in result.output


def test_happy_path_dispatch_reap_pr_merge(sched, fake_github):
    # Exercise the plain dependency gate (dep must be *done*), not stacking: with stacking on,
    # DM-002 would stack-dispatch onto DM-001's open PR at tick 2, and this test's final
    # "DM-002 running after merge" assertion would then depend on whether that stacked worker
    # happened to still be running at the merge tick — a timing race that flaked in CI. Stacking
    # has its own coverage (see test_feedback_triggers_revise_round); here we want the merge to
    # be what unblocks and dispatches DM-002. See CG-119.
    sched.cfg.data["stack"] = False
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # DM-002 is blocked
    assert statuses(sched)["DM-001"] == "running"
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created and fake_github.created[0]["title"] == "Fake: implemented the thing"
    assert fake_github.created[0]["head"] == "garden/dm-001-first-task"
    t = sched.store.task("DM-001")
    assert t.pr.endswith("/pull/101") and t.attempts == 1
    # the branch was pushed to origin
    remote = sched.repo_for(t).parent / "remote.git"
    out = subprocess.run(["git", "branch", "--list", "garden/*"], cwd=remote, capture_output=True, text=True).stdout
    assert "garden/dm-001-first-task" in out
    run = sched.runs.latest("DM-001")
    assert run.status == "done" and run.cost_usd == 0.05 and run.usage["input_tokens"] == 1234
    assert (run.path / "brief.md").exists()
    assert "Do the first thing" in (run.path / "references/context/task.md").read_text()

    # nothing new on the PR -> no change
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    # merge -> done, and DM-002 (blocked until now) unblocks and dispatches in the same tick
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    s = statuses(sched)
    assert s["DM-001"] == "done" and s["DM-002"] == "running"
    assert not sched.worktree_for(sched.store.task("DM-001")).exists()


def test_design_dependency_waits_for_merge_instead_of_stacking(sched):
    """A design parent with an open PR does not make its child dispatchable."""
    sched.cfg.data["stack"] = True
    parent = sched.store.task("DM-001")
    parent.kind = "design"
    child = sched.store.task("DM-002")
    sched.store.save(parent)
    sched.store.save(child)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]
    rep = sched.tick()
    assert "DM-002(work)" not in rep.dispatched
    assert statuses(sched)["DM-002"] == "ready"


def test_design_dependency_can_explicitly_override_default_with_stack(sched):
    sched.cfg.data["stack"] = True
    parent = sched.store.task("DM-001")
    parent.kind = "design"
    child = sched.store.task("DM-002")
    child.dependency_after[parent.id] = "stack"
    sched.store.save(parent)
    sched.store.save(child)
    sched.tick()
    rep = sched.tick()
    assert "DM-002(work)" in rep.dispatched


def test_design_context_is_run_scoped_and_referenced_without_dirtying_checkout(sched):
    from tests.conftest import git

    task = sched.store.task("DM-001")
    task.title = "Design the queue UI"
    sched.store.save(task)
    repo = sched.repo_for(task)
    tracked = repo / "docs" / "design" / "snapshot.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text('{"curated": true}\n')
    git("add", "docs/design/snapshot.json", cwd=repo)
    git("commit", "-q", "-m", "add curated snapshot", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)

    run = sched.dispatch(task)

    context = run.path / "design-context.json"
    assert context.exists()
    brief = (run.path / "brief.md").read_text()
    assert "$GARDEN_CONTEXT_DIR/context/evidence/design-context.json" in brief
    assert (run.path / "references/context/evidence/design-context.json").exists()
    assert (Path(run.worktree) / "docs" / "design" / "snapshot.json").read_text() == '{"curated": true}\n'
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=run.worktree, capture_output=True, text=True, check=True,
    ).stdout == ""


def test_rebase_does_not_generate_design_context(sched):
    task = sched.store.task("DM-001")
    task.title = "Design the queue UI"
    task.status = Status.IN_REVIEW
    task.pr = "https://example.test/pull/1"
    sched.store.save(task)

    run = sched.dispatch(task, mode="rebase")

    assert not (run.path / "design-context.json").exists()
    assert "Generated design context" not in (run.path / "brief.md").read_text()


def test_brief_drops_reading_not_present_at_the_task_base(sched, tmp_path):
    """A branch-only file is not context at the task's base and must not leak into its brief."""
    from tests.conftest import git, write

    repo = tmp_path / "repo"
    branch = "garden/dm-001-first-task"  # DM-001's default branch
    # Commit a file only on the task's branch; main never gets it.
    git("checkout", "-q", "-b", branch, cwd=repo)
    write(repo / "src" / "onbranch.py", "ANSWER = 42\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add onbranch", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)

    t = sched.store.task("DM-001")
    t.reading = ["src/onbranch.py"]
    sched.store.save(t)
    sched.store.invalidate()

    sched.tick()  # dispatch DM-001 (the in-process worker runs synchronously)
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "### src/onbranch.py" not in brief and "ANSWER = 42" not in brief
    assert "## Brief gaps" in brief and "- `src/onbranch.py`" in brief


def test_revise_brief_names_rebase_conflict_without_github_feedback(sched):
    from garden.model import Status

    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.test/pull/1"
    sched.store.save(task)
    state = sched.state.get(task.id)
    state["pending_feedback_rebase"] = True
    sched.dispatch(task, mode="revise")
    brief = (sched.runs.latest(task.id).path / "brief.md").read_text()
    assert "context/review-findings.md" in brief
    findings = sched.runs.latest(task.id).path / "references/context/review-findings.md"
    assert "GitHub has no open review comments" in findings.read_text()
    assert "rebase conflict" in findings.read_text()


def test_oversized_prompt_is_rejected_before_the_runner_starts(sched, monkeypatch):
    """The scheduler must not spend a turn on a harness input it already knows is invalid."""
    task = sched.store.task("DM-001")
    runner = sched.runner_for(task)
    started = False

    def start(*args, **kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr(runner, "start", start)
    with pytest.raises(ValueError, match="serialized prompt"):
        sched.dispatch(task, prompt_override="x" * (MAX_SERIALIZED_PROMPT_BYTES + 1))
    assert not started


def test_large_invalid_utf8_rebase_conflict_recovers_once_and_preserves_stages(sched, monkeypatch):
    """A real oversized invalid-UTF-8 conflict recovers unattended, retaining Git's exact stages."""
    from garden import gitops
    from tests.conftest import git

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.test/pull/1"
    task.branch = task.default_branch()
    sched.store.save(task)
    path = "docs/design/snapshot.json"
    repo = sched.repo_for(task)
    wt = sched.worktree_for(task)

    # Invalid UTF-8 proves the artifacts come from Git's index stages, not a decoded worktree
    # rendering, while the large generated payload forces the bounded-summary path.
    original = b"original\xff\n"
    main_blob = b"main\xff\n" + b"m" * (1024 * 1024 + 128)
    branch_blob = b"branch\xfe\n" + b"b" * (1024 * 1024 + 256)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)
    git("add", path, cwd=repo)
    git("commit", "-q", "-m", "add generated snapshot", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)

    gitops.prepare_worktree(repo, wt, task.branch, "main")
    (wt / path).write_bytes(branch_blob)
    git("add", path, cwd=wt)
    git("commit", "-q", "-m", "branch snapshot", cwd=wt)
    git("push", "-q", "-u", "origin", task.branch, cwd=wt)

    target.write_bytes(main_blob)
    git("add", path, cwd=repo)
    git("commit", "-q", "-m", "main snapshot", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)

    outcome = sched._rebase_and_record(task, "main", wt=wt)
    assert outcome.status == "conflict"
    assert outcome.files == [path]
    # Make the actual rebase dispatch salvage a dirty worktree before the resolving agent runs.
    (wt / "leftover.txt").write_text("preserve me\n")
    sched._dispatch_rebase_agent(task, "main", outcome.files, outcome.hunks, outcome.artifacts,
                                 TickReport(), "fixture")
    state = sched.state.get(task.id)
    artifact = state["rebase_artifacts"][path]
    stages = {entry["stage"]: entry for entry in artifact["stages"]}
    assert Path(stages[1]["path"]).read_bytes() == original
    # During a rebase Git's stage 2 is the onto/base side and stage 3 is the rebased commit.
    assert Path(stages[2]["path"]).read_bytes() == main_blob
    assert Path(stages[3]["path"]).read_bytes() == branch_blob
    assert all(len(entry["sha256"]) == 64 for entry in stages.values())

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "rebase-resolve")
    run = sched.dispatch(task, mode="rebase")
    brief = (run.path / "brief.md").read_text()
    assert len(brief.encode("utf-8")) <= MAX_SERIALIZED_PROMPT_BYTES
    assert "Large conflict omitted from this prompt" in brief
    assert str(stages[2]["path"]) in brief
    assert "b" * 128 not in brief
    assert state["stashes"][-1]["sha"] and state["stashes"][-1]["reason"] == "pre-dispatch"

    sched.tick()  # reap the one resolving agent
    sched.tick()  # continue through the ordinary post-rebase poll/dispatch path
    agent_runs = [item for item in sched.runs.runs_for(task.id) if item.mode == "rebase"]
    assert len(agent_runs) == 1
    assert not state.get("rebase_run_retries") and not state.get("needs_human")
    assert sched.store.task(task.id).status == Status.IN_REVIEW


def test_redispatched_work_brief_lists_prior_commits(sched):
    """CG-125: a fresh work round that lands on a worktree an interrupted attempt left with
    commits gets those commits listed in its brief ("Already on this branch"), so it builds
    on the prior progress instead of reverse-engineering it from git log/diff."""
    from garden import gitops
    from tests.conftest import git, write

    sched.cfg.data["stack"] = False
    # Leave commits from an interrupted attempt on the branch worktree, with no live run.
    task = sched.store.task("DM-001")
    wt = sched.worktree_for(task)
    gitops.prepare_worktree(sched.repo_for(task), wt, task.default_branch(), "main")
    write(wt / "progress.txt", "partial\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "DM-001: partial fix", cwd=wt)
    assert gitops.commits_ahead(wt, "main") == 1

    rep = sched.tick()  # dispatch a fresh work round onto the worktree with prior commits
    assert "DM-001(work)" in rep.dispatched
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "## Already on this branch" in brief and "partial fix" in brief


def test_max_parallel(sched, garden):
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    rep = sched.tick()
    assert len(rep.dispatched) == 2


def test_garden_yaml_reloaded_between_ticks(sched, garden):
    """CG-192: editing garden.yaml is honoured within one tick, no restart. The scheduler
    re-reads store.config each pass when the file's mtime changed, so a raised max_parallel
    dispatches more work on the very next tick and the change is logged."""
    import os

    import yaml

    logs = []
    sched.log = logs.append
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    rep = sched.tick()
    assert len(rep.dispatched) == 2  # garden.yaml's max_parallel

    # raise the limit in garden.yaml (and bump the mtime so the reload is deterministic)
    p = garden / "garden.yaml"
    data = yaml.safe_load(p.read_text())
    data["max_parallel"] = 4
    p.write_text(yaml.safe_dump(data))
    future = os.stat(p).st_mtime + 10
    os.utime(p, (future, future))

    rep = sched.tick()  # reaps the 2 finished runs, re-reads the config, dispatches up to 4
    assert sched.cfg.get("max_parallel") == 4
    assert len(rep.dispatched) == 3  # the 3 remaining ready tasks, without a restart
    assert any("garden.yaml reloaded" in m and "max_parallel" in m for m in logs)


def test_max_parallel_override_and_clear(sched):
    from garden.scheduler import State

    assert sched.effective_max_parallel() == 2  # garden.yaml's max_parallel, no override yet
    assert sched.overrides().get("max_parallel") is None
    sched.set_override("max_parallel", 7, by="test")
    assert sched.effective_max_parallel() == 7
    on_disk = State(sched.state.path).get("_control")
    assert on_disk["overrides"]["max_parallel"] == 7
    sched.clear_override("max_parallel", by="test")
    assert sched.effective_max_parallel() == 2  # back to the garden.yaml value
    on_disk = State(sched.state.path).get("_control")
    assert "max_parallel" not in on_disk.get("overrides", {})


def test_plain_inherited_lock_rejects_runtime_and_profile_changes(sched):
    sched.cfg.data["products"]["demo"]["configuration"] = {
        "locks": {"max_parallel": {"reason": "fixed capacity"}},
    }
    with pytest.raises(PermissionError, match="fixed capacity"):
        sched.set_override("max_parallel", 7)
    with pytest.raises(PermissionError, match="fixed capacity"):
        sched.set_operating_profile("fast")
    assert sched.effective_max_parallel() == 2


def test_tick_uses_max_parallel_override(sched, garden):
    """The override takes effect on the very next tick, no restart, and running workers
    are never stopped by lowering it (dispatch just skips them until the count drops)."""
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    sched.set_override("max_parallel", 1)
    rep = sched.tick()
    assert len(rep.dispatched) == 1  # override (1) wins over garden.yaml's max_parallel (2)
    sched.clear_override("max_parallel")
    rep = sched.tick()  # reaps the finished run, then dispatches up to garden.yaml's 2 again
    assert len(rep.dispatched) == 2


def test_review_runs_do_not_consume_worker_slots(sched):
    """max_parallel=2 (fixture). A review run must not count against it, and review_parallel
    (defaulting to max_parallel) must be tracked independently."""
    t = sched.store.task("DM-001")
    review_run = sched.runs.new_run(t.id, "local", mode="review")
    review_run.status = "running"
    review_run.save()
    assert sched.slots_free() == 2  # the review run holds a review slot, not a worker slot
    assert sched.review_parallel_limit() == 2  # defaults to max_parallel
    assert sched.review_slots_free() == 1

    work_run = sched.runs.new_run(t.id, "local", mode="work")
    work_run.status = "running"
    work_run.save()
    assert sched.slots_free() == 1
    assert sched.review_slots_free() == 1  # unaffected by the work run

    sched.cfg.data["review_parallel"] = 1
    assert sched.review_parallel_limit() == 1
    assert sched.review_slots_free() == 0


def test_checks_and_edits_do_not_consume_worker_slots(sched):
    """The max_parallel count is the same worker-only count shown by the UI and CLI.
    A check in flight must not prevent a ready task from taking a worker slot."""
    check_run = sched.runs.new_run("DM-001", "local", mode="check")
    check_run.status = "running"
    check_run.save()
    edit_run = sched.runs.new_run("DM-002", "local", mode="edit")
    edit_run.status = "running"
    edit_run.save()

    assert len(sched.check_runs_active()) == 1
    assert sched.slots_free() == 2
    from garden.now1 import render_text, snapshot

    snap = snapshot(sched.store, sched)
    assert snap["garden"]["worker_busy"] == 0
    assert "0 of 2 worker slots" in render_text(snap)
    rep = sched.tick()
    assert "DM-001(work)" in rep.dispatched
    assert len(sched.worker_runs_active()) == 1
    assert sched.slots_free() == 1


def test_edit_dispatch_is_not_blocked_by_full_worker_cap(sched):
    """An edit is slot-free, so pending suggestions are integrated even when workers are full."""
    worker = sched.runs.new_run("DM-001", "local", mode="work")
    worker.status = "running"
    worker.save()
    sched.set_override("max_parallel", 1)

    task = sched.store.task("DM-002")
    record_suggestion(sched.store, task, "cover the empty case", author="josh")

    from garden.scheduler import TickReport

    rep = TickReport()
    sched.dispatch_edits(rep)

    assert sched.slots_free() == 0
    assert "DM-002(edit)" in rep.dispatched
    assert sched.state.get("DM-002").get("edit_run")


def test_pause_stops_dispatch(sched, fake_github):
    sched.pause(by="cli", reason="testing")
    assert sched.is_dispatch_paused()
    rep = sched.tick()
    # nothing dispatched while paused
    assert rep.dispatched == []
    assert statuses(sched)["DM-001"] == "ready"


def test_resume_restarts_dispatch(sched, fake_github):
    sched.pause(by="cli")
    sched.tick()
    assert statuses(sched)["DM-001"] == "ready"
    sched.resume(by="cli")
    assert not sched.is_dispatch_paused()
    rep = sched.tick()
    assert "DM-001(work)" in rep.dispatched


def test_paused_tick_still_reaps_and_polls(sched, fake_github):
    # dispatch, let worker finish, then pause before reaping
    sched.tick()
    sched.pause(by="cli")
    rep = sched.tick()
    # should reap the finished worker and push the PR even while paused
    assert "DM-001" in rep.reaped
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created  # PR was opened
    # poll should also run: merge the PR -> task becomes done
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "done"
    # DM-002 remains ready (not dispatched because paused)
    assert statuses(sched)["DM-002"] == "ready"


def test_pause_state_persists_in_state_json(sched, fake_github):
    sched.pause(by="cli", reason="hold")
    path = sched.state.path
    from garden.scheduler import State
    fresh = State(path).get("_control")
    assert fresh["dispatch"] == "paused" and fresh["reason"] == "hold" and fresh["by"] == "cli"


def test_empty_maintenance_read_cannot_overwrite_concurrent_request(sched):
    from garden.scheduler import Scheduler
    from garden.store import Store

    assert not sched.maintenance_requested()
    assert "maintenance" not in sched.control()
    assert not sched.state.get("_control").dirty

    newer = Scheduler(Store(sched.store.root))
    newer.request_maintenance_pause(by="test", reason="concurrent reinstall")
    sched.control()["unrelated_tick_note"] = "saved after maintenance request"
    sched.state.save()

    fresh = Scheduler(Store(sched.store.root))
    assert fresh.maintenance_requested()
    assert fresh.control()["maintenance"]["reason"] == "concurrent reinstall"
    assert fresh.control()["unrelated_tick_note"] == "saved after maintenance request"
    fresh.tick()
    assert fresh.maintenance_quiesced()
    fresh.resume_maintenance(by="test")
    assert not Scheduler(Store(sched.store.root)).maintenance_requested()


def test_maintenance_pause_freezes_collection_until_explicit_resume(sched, fake_github):
    """A finished worker result remains durable through a scheduler restart boundary."""
    sched.tick()  # start DM-001's fake worker
    run = sched.latest_worker_run("DM-001")
    assert run is not None and run.status == "running"

    sched.request_maintenance_pause(by="test", reason="reinstall")
    status = sched.maintenance_readiness()
    assert status["requested"] and not status["quiesced"]
    sched.tick()  # acknowledgement pass: must not reap the result
    assert sched.maintenance_quiesced()
    assert sched.store.task("DM-001").status == Status.RUNNING
    assert sched.latest_worker_run("DM-001").status == "running"

    with pytest.raises(RuntimeError, match="maintenance pause"):
        sched.dispatch(sched.store.task("DM-002"))
    sched.resume_maintenance(by="test")
    sched.tick()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW


def test_maintenance_readiness_keeps_uncollected_result_separate_from_live_blockers(sched):
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "local", mode="work")
    run.status = "done"
    run.finished_at = "2026-01-01T00:00:00+00:00"
    run.save()
    sched.request_maintenance_pause(by="test")
    sched.tick()
    status = sched.maintenance_readiness()
    assert status["ready"]
    assert run.run_id in status["finished_uncollected"]

    live = sched.runs.new_run("DM-002", "local", mode="work")
    live.status = "running"
    live.pid = 12345
    live.save()
    status = sched.maintenance_readiness()
    assert not status["ready"]
    assert "installed runtime" in status["live"][0]["blocker"]


def test_maintenance_readiness_allows_a_probed_dormant_legacy_ssh_run(sched):
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "ssh", mode="work")
    run.status = "running"
    run.pid = 987654321
    run.env_snapshot["ssh_tmux_session"] = "garden-DM-001-legacy"
    run.save()
    (run.path / "ssh-state.json").write_text(json.dumps({
        "status": "held",
        "held_kind": "legacy_artifacts_present",
        "process_exists": False,
        "artifacts": {
            "directory_exists": True,
            "worktree_exists": True,
            "session_exists": False,
        },
    }))
    sched.request_maintenance_pause(by="test")
    sched._quiesce_for_maintenance()

    status = sched.maintenance_readiness()
    assert status["ready"]
    assert status["live"] == []
    assert status["dormant"] == [f"{task.id}/{run.run_id}"]


def test_pause_overrides_auto_dispatch_true(sched, fake_github):
    sched.cfg.data["auto_dispatch"] = True
    sched.pause(by="web")
    rep = sched.tick()
    assert rep.dispatched == []


def test_enforced_project_auto_dispatch_policy_changes_scheduler_decision(sched):
    sched.cfg.data["auto_dispatch"] = True
    sched.cfg.data["products"]["demo"]["configuration"] = {
        "overrides": {},
        "locks": {"auto_dispatch": {"reason": "release hold", "value": False}},
    }

    rep = sched.tick()

    assert rep.dispatched == []
    assert sched.store.task("DM-001").status == Status.READY

    sched.cfg.data["auto_dispatch"] = False
    sched.cfg.data["products"]["demo"]["configuration"]["locks"]["auto_dispatch"]["value"] = True
    rep = sched.tick()
    assert "DM-001(work)" in rep.dispatched


def test_audit_flags_stuck_changes_requested(sched, fake_github):
    """A task hand-left in changes_requested with no feedback, no run and no needs_human
    is stuck: the tick audit must give it an attention card, not let it sit silent."""
    from garden.inbox import build_inbox

    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = ""  # nothing to revise against -> not dispatchable
    st["revisions"] = 0
    sched.state.save()

    rep = sched.tick()
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "no feedback" in st["needs_human"]
    assert any("stuck" in tr for tr in rep.transitions)
    # it now appears on the Inbox attention group
    cards = [it for it in build_inbox(sched.store, sched) if it["task"] == "DM-001"]
    assert any(c["group"] == "attention" and "stuck" in c["why"] for c in cards)


def test_audit_flags_pending_feedback_stuck_on_in_review(sched, fake_github):
    """CG-140: a stored pending_feedback always comes with the changes_requested
    transition; if it ever appears on an in_review task instead (no run dispatches from
    there), the tick audit must flag it rather than let automerge hold on it silently."""
    t = sched.store.task("DM-001")
    t.status = Status.IN_REVIEW
    t.pr = "https://example.com/pr/1"
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- **automated review** PR description: needs work"
    sched.state.save()

    rep = sched.tick()
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "in_review" in st["needs_human"]
    assert any("stuck" in tr for tr in rep.transitions)


def test_audit_does_not_flag_dispatchable_changes_requested(sched, fake_github):
    """A changes_requested task with feedback under the cap is dispatchable; not stuck."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()
    sched.tick(dispatch=False)  # audit runs even with dispatch off
    assert not sched.state.get("DM-001").get("needs_human")


def test_audit_flags_manual_task_with_a_revise_round_waiting(sched, fake_github):
    """CG-158: a `runner: manual` task sent to changes_requested is never auto-dispatched
    (dispatch_ready skips non-detached runners) — the stuck audit must not mistake its
    waiting feedback for something the queue will pick up, or it rots silently with no
    Inbox card and nothing telling anyone to `garden take` it."""
    t = sched.store.task("DM-001")
    t.runner = "manual"
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()

    rep = sched.tick()
    assert rep.dispatched == []  # never auto-dispatched
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "manual" in st["needs_human"]


def test_take_clears_a_stale_needs_human_flag(sched, fake_github):
    """Taking a manual task that the stuck audit flagged is the human resolving the flag,
    the same way retry/resume/answer already do: the Inbox card must not linger once a
    fresh run is actually in flight."""
    from garden.runner.manual import ManualRunner

    t = sched.store.task("DM-001")
    t.runner = "manual"
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()
    sched.tick()
    assert sched.state.get("DM-001").get("needs_human")

    sched.dispatch(sched.store.task("DM-001"), mode="revise", runner=ManualRunner({}), worktree=False)
    assert not sched.state.get("DM-001").get("needs_human")


def test_dispatch_ready_failure_does_not_abort_the_tick(sched, fake_github, monkeypatch):
    """CG-203: dispatch_ready is wrapped in the same guard as every other tick phase — an
    exception is logged with the phase name and the tick still runs the phases after it
    (audit), instead of the exception escaping tick() and skipping everything past it."""
    sched.cfg.data["stack"] = False

    def boom(rep):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched, "dispatch_ready", boom)
    rep = sched.tick()
    assert any("dispatch ready failed" in e and "boom" in e for e in rep.errors)
    assert "audit" in rep.steps  # the tick kept going past the failing phase


def test_no_dispatch_tick_reuses_one_snapshot_and_refreshes_next_pass(sched, garden, monkeypatch):
    """A quiet pass scans once; a later controller pass sees another writer's status."""
    from garden.store import Store

    scans = 0
    original_scan = Store._scan

    def counted_scan(self):
        nonlocal scans
        scans += 1
        return original_scan(self)

    monkeypatch.setattr(Store, "_scan", counted_scan)

    sched.tick(dispatch=False)
    assert scans == 1

    writer = Store(garden)
    task = writer.task("DM-001")
    task.status = Status.WAITING_HUMAN
    writer.save(task)

    scans = 0
    sched.tick(dispatch=False)
    assert scans == 1
    assert sched.store.task("DM-001").status == Status.WAITING_HUMAN


def test_tick_refreshes_snapshot_after_its_own_task_mutation(sched, monkeypatch):
    """A supported transition invalidates the remainder of its tick's task snapshot."""
    seen = []

    def transition_during_reap(rep):
        task = sched.store.task("DM-001")
        sched._transition(task, Status.WAITING_HUMAN, "test snapshot refresh")

    def observe_status(task, rep):
        seen.append(task.status)

    monkeypatch.setattr(sched, "_reap_all", transition_during_reap)
    monkeypatch.setattr(sched, "_reprobe_base_broken", observe_status)

    sched.tick(dispatch=False)

    assert seen == [Status.WAITING_HUMAN, Status.READY]


def test_exception_in_dispatch_does_not_lose_an_earlier_transition(sched, fake_github, monkeypatch):
    """CG-203: state.save() runs in a `finally`, so a state.json field an earlier phase in the
    same tick wrote — here, the PR-open reap's pr_number cache — is not lost when a later
    phase (dispatch) blows up before the tick would otherwise have saved it."""
    from garden.scheduler import State

    sched.cfg.data["stack"] = False
    sched.tick()  # dispatch DM-001 (the in-process worker finishes synchronously)

    def boom(rep):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched, "dispatch_ready", boom)
    rep = sched.tick()  # reaps the finished run (opens the PR, caches pr_number), then dispatch blows up
    assert statuses(sched)["DM-001"] == "in_review"
    assert any("dispatch ready failed" in e for e in rep.errors)

    fresh = State(sched.state.path).get("DM-001")  # reload from disk, not the in-memory copy
    assert fresh.get("pr_number")  # persisted despite the exception in dispatch


def test_dispatch_preparation_failure_closes_created_run(sched, monkeypatch):
    """Any preparation error after run creation closes the run in the same dispatch."""
    task = sched.store.task("DM-001")

    def boom(_task):
        raise RuntimeError("broken setup")

    monkeypatch.setattr(sched, "_stack_for", boom)
    with pytest.raises(RuntimeError, match="broken setup"):
        sched.dispatch(task)

    run = sched.runs.latest(task.id)
    assert run is not None
    assert run.status == "failed" and run.finished_at and run.error == "broken setup"
    finished = [e for e in sched.events.read(task_id=task.id, kinds=["run_finished"])
                if e.get("run") == run.run_id]
    assert len(finished) == 1 and finished[0]["status"] == "failed"


def test_dispatch_start_failure_after_launch_keeps_run_running(sched, monkeypatch):
    """A runner that launches a process before reporting a startup error still owns the run."""
    task = sched.store.task("DM-001")
    runner = sched.runner_for(task)

    def launched_then_failed(run, _worktree, _brief):
        run.pid = os.getpid()
        run.save()
        raise RuntimeError("failed while recording startup")

    monkeypatch.setattr(runner, "start", launched_then_failed)
    with pytest.raises(RuntimeError, match="failed while recording startup"):
        sched.dispatch(task, runner=runner)

    run = sched.runs.latest(task.id)
    assert run is not None
    assert run.status == "running" and run.pid == os.getpid()
