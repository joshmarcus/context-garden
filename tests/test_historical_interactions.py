"""Focused served regressions retained from historical interaction receipts."""

from __future__ import annotations

import httpx

from garden.model import Status
from garden.qa.sandbox import start
from garden.runs import RunStore
from garden.scheduler import Scheduler, State
from garden.store import Store


def _client(box: object) -> httpx.Client:
    base_url = getattr(box, "base_url")
    return httpx.Client(base_url=base_url, follow_redirects=False, timeout=20,
                        headers={"referer": base_url + "/inbox"})


def test_cg381_inbox_ownership_and_deployment_recovery_are_served(tmp_path):
    """The former CG-381 capture's actions stay reproducible without its script."""
    def prepare(root):
        store = Store(root)
        review = store.task("DM-001")
        review.status = Status.IN_REVIEW
        review.pr = "https://example.invalid/qa/demo/pull/71"
        store.save(review)
        recovery = store.task("DM-002")
        recovery.status = Status.FAILED
        store.save(recovery)
        state = State(root / ".garden" / "state.json")
        state.get("DM-001").update({
            "head_sha": "head", "last_review_head": "head",
            "last_review": {"verdict": "request_changes", "summary": "add a boundary test"},
            "pending_reviews": [{"kind": "review"}],
        })
        state.get("DM-002")["needs_human"] = {
            "kind": "deployment", "reason": "deploy the verified build",
            "prior_status": "in_review", "at": "2026-09-07T00:00:00+00:00",
        }
        state.save()

    box = start(tmp_path / "cg381", prepare=prepare, watch=False)
    try:
        with _client(box) as client:
            inbox = client.get("/inbox")
            assert inbox.status_code == 200
            assert '<div class="v">0</div><div class="l">need you</div>' in inbox.text
            assert "automated review queued" in inbox.text
            assert "Operator recovery: Deployment prerequisite" in inbox.text
            assert client.post("/tasks/DM-999/resume").status_code == 404
            assert client.post("/tasks/DM-002/resume").status_code == 303
            recovered = client.get("/inbox")
            assert "Operator recovery: Deployment prerequisite" not in recovered.text
            assert "automated review queued" in recovered.text
    finally:
        box.stop()


def test_cg410_manual_claim_stale_take_and_completion_are_served(tmp_path):
    """The former CG-410 capture's manual-session safety remains a focused regression."""
    def prepare(root):
        store = Store(root)
        for task_id in ("DM-001", "DM-002"):
            task = store.task(task_id)
            task.runner = "manual"
            store.save(task)
        runs = RunStore(root / ".garden")
        for _ in range(4):
            runs.new_run("DM-002", "local", "work")

    box = start(tmp_path / "cg410", prepare=prepare, watch=False)
    try:
        with _client(box) as client:
            inbox = client.get("/inbox")
            assert "Manual work ready" in inbox.text
            assert "Take task" in inbox.text
            assert 'action="/tasks/DM-002/take"' not in client.get("/tasks/DM-002").text
            assert Scheduler(Store(box.garden)).slots_free() == 0
            assert client.post("/tasks/DM-001/take").status_code == 303
            assert client.get("/tasks/DM-001/packet").status_code == 200
            assert client.post("/tasks/DM-001/take").status_code == 409
            assert client.post("/tasks/DM-001/finish-manual", data={"note": "not JSON"}).status_code == 303
            assert client.post(
                "/tasks/DM-001/finish-manual",
                data={"note": '{"status":"blocked","summary":"waiting on access"}'},
            ).status_code == 303
            assert Store(box.garden).task("DM-001").status == Status.FAILED
    finally:
        box.stop()
