from __future__ import annotations

from pathlib import Path

from garden.model import Status
from garden.personas import parse_persona

FAKE_OPENROUTER = Path(__file__).with_name("fake_openrouter.py")
MODEL = "openrouter/qwen/qwen3-coder"


def _configure(sched, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    sched.cfg.data.setdefault("harnesses", {})["openrouter"] = {
        "bin": str(FAKE_OPENROUTER),
        "models": {"easy": MODEL, "medium": MODEL, "hard": MODEL},
        "max_turns": {"easy": 20, "medium": 40, "hard": 80},
    }


def _complete_work(sched, monkeypatch):
    _configure(sched, monkeypatch)
    task = sched.store.task("DM-001")
    task.harness = "openrouter"
    sched.store.save(task)
    sched.tick()
    work = sched.runs.latest(task.id)
    sched.tick()
    return sched.store.task(task.id), work


def _assert_openrouter_artifacts(run):
    refreshed = type(run).load(run.path)
    assert refreshed.status == "done"
    assert refreshed.usage["input_tokens"] == 100
    assert refreshed.cost_usd == 0.0042
    assert (run.path / "stdout.json").read_text().strip()
    assert (run.path / "final.md").read_text().strip()
    assert (run.path / "run.json").exists()
    return refreshed


def test_openrouter_work_run_persists_transcript_final_usage_and_cost(sched, monkeypatch):
    task, work = _complete_work(sched, monkeypatch)
    assert task.status == Status.IN_REVIEW
    work = _assert_openrouter_artifacts(work)
    assert work.result["status"] == "done"


def test_openrouter_review_run_completes_through_normal_review_path(sched, monkeypatch):
    task, work = _complete_work(sched, monkeypatch)
    sched.cfg.data["review"]["harness"] = "openrouter"
    review = sched.dispatch_review(task, work)
    sched.tick()
    review = _assert_openrouter_artifacts(review)
    assert review.result["verdict"] == "approve"


def test_openrouter_persona_run_completes_through_normal_persona_path(sched, monkeypatch):
    task, _work = _complete_work(sched, monkeypatch)
    sched.cfg.data["review"]["harness"] = "openrouter"
    persona = sched.dispatch_persona_pr(task, "security")
    sched.tick()
    persona = _assert_openrouter_artifacts(persona)
    assert parse_persona((persona.path / "final.md").read_text())["score"] == 9
