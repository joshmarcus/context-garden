import subprocess

from garden.model import Status
from tests.conftest import wait_for_runs


def test_codex_harness_and_difficulty_model(sched, garden, fake_github):
    t = sched.store.task("DM-001")
    t.harness = "codex"
    t.difficulty = "hard"
    sched.store.save(t)
    sched.tick()
    wait_for_runs(sched)
    run = sched.runs.latest("DM-001")
    assert run.harness == "codex" and run.model == "gpt-max"
    sched.tick()
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    run = sched.runs.latest("DM-001")
    assert run.usage["input_tokens"] == 500 and run.result["summary"] == "codex did it with gpt-max"
    assert fake_github.created[0]["title"] == "Codex PR"


def test_explicit_model_override(sched):
    t = sched.store.task("DM-001")
    t.model = "my-model"
    sched.store.save(t)
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.model == "my-model"
    wait_for_runs(sched)
    assert (sched.worktree_for(t) / "model.txt").read_text().strip() == "my-model"


def test_easy_task_gets_cheap_model(sched):
    t = sched.store.task("DM-001")
    t.difficulty = "easy"
    sched.store.save(t)
    sched.tick()
    assert sched.runs.latest("DM-001").model == "haiku"


def test_ssh_runner_end_to_end(sched, garden, fake_github, tmp_path):
    t = sched.store.task("DM-001")
    t.runner = "ssh"
    sched.store.save(t)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]
    run = sched.runs.latest("DM-001")
    assert run.runner == "ssh" and run.host == "boxA" and run.worktree == ""
    assert "git push" in (run.path / "remote.sh").read_text()
    wait_for_runs(sched)
    assert (run.path / "exit_code").read_text().strip() == "0", (run.path / "stderr.log").read_text()
    rep = sched.tick()
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW, rep.summary()
    # the remote clone pushed the branch to origin, and a local worktree was materialised for review
    remote = tmp_path / "remote.git"
    out = subprocess.run(["git", "branch", "--list", "garden/*"], cwd=remote, capture_output=True, text=True, check=False).stdout
    assert "garden/dm-001-first-task" in out
    assert (sched.worktree_for(t) / "worker-output.txt").exists()
    assert fake_github.created[0]["head"] == "garden/dm-001-first-task"


def test_ssh_host_capacity(sched):
    for tid in ("DM-001", "DM-002"):
        t = sched.store.task(tid)
        t.runner = "ssh"
        t.depends_on = []
        sched.store.save(t)
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # boxA max_parallel 1
    assert any("max_parallel" in e for e in rep.errors)
