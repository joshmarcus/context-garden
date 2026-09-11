import sys

import pytest

from garden.worker_rollout import (
    CommandWorkerRolloutBackend,
    PublishedVersion,
    RolloutStore,
    WorkerRollout,
    WorkerTarget,
)


def candidate(**changes):
    values = {"version": "1.2.3", "source_commit": "a" * 40,
              "manifest": {"garden.py": "b" * 64}, "verified": True}
    values.update(changes)
    return PublishedVersion(**values)


def worker(name="worker-1", **changes):
    values = {"worker_id": name, "host_id": "host-1", "owner": "garden",
              "group": "garden", "config_path": "/etc/garden/worker.json",
              "unit_source": "/etc/systemd/system/garden-worker.service", "unit_mode": "0644",
              "resource_caps": {"memory_mib": 4096}, "deadline": "2026-10-01T00:00:00Z",
              "enrollment_boundary": "worker-token", "secret_boundary": "/run/secrets"}
    values.update(changes)
    return WorkerTarget(**values)


class Backend:
    def __init__(self):
        self.generation = "idle-1"
        self.busy = False
        self.pending = False
        self.stages = 0
        self.activations = 0
        self.health_calls = 0
        self.rollback_ok = True
        self.stage_changes = {}
        self.active_changes = {}
        self.health_changes = {}
        self.observe_hook = None
        self.fenced = []

    def observe(self, target):
        if self.observe_hook:
            self.observe_hook(self)
        return {"busy": self.busy, "pending_collection": self.pending,
                "claim_generation": self.generation, "runtime": "/opt/garden/old"}

    def stage(self, target, release):
        self.stages += 1
        return {"runtime": "/opt/garden/1.2.3", "version": release.version,
                "direct_url_commit": release.source_commit, "manifest": dict(release.manifest),
                "executable": "/opt/garden/1.2.3/bin/garden", "tools_ok": True,
                **self.stage_changes}

    def activate(self, target, release, idle_generation):
        self.activations += 1
        return {"version": release.version, "source_commit": release.source_commit,
                "direct_url_commit": release.source_commit, "manifest": dict(release.manifest),
                "runtime": "/opt/garden/1.2.3",
                "config_path": target.config_path, "owner": target.owner, "group": target.group,
                "unit_mode": target.unit_mode, "unit_source": target.unit_source, "pid": 22,
                "prior_pid": 11, "frozen": False, "ready": True,
                "executable": "/opt/garden/1.2.3/bin/garden",
                "runtime_executable": "/opt/garden/1.2.3/bin/garden", **self.active_changes}

    def health(self, target, release):
        self.health_calls += 1
        return {"authenticated": True, "claim_probe": True, "heartbeat_probe": True,
                "finish_probe": True, "ready": True, "restarted": False,
                "resource_failure": False, "version": release.version,
                "source_commit": release.source_commit, **self.health_changes}

    def rollback(self, target, runtime, idle_generation):
        return {"ok": self.rollback_ok, "runtime": runtime, "ready": self.rollback_ok}

    def fence(self, target, reason):
        self.fenced.append((target.worker_id, reason))


def operation(tmp_path, backend=None, **options):
    backend = backend or Backend()
    rollout = WorkerRollout(RolloutStore(tmp_path / "rollout.json"), backend,
                            sleep=lambda _: None, **options)
    rollout.plan(candidate(), [worker()], aggregate_budget=80, controller_runtime="/opt/controller")
    return rollout, backend


@pytest.mark.parametrize("changes", [
    {"verified": False}, {"manifest": {}}, {"source_commit": "main"}, {"version": "latest"},
])
def test_plan_rejects_mutable_or_partial_candidates(tmp_path, changes):
    rollout = WorkerRollout(RolloutStore(tmp_path / "r.json"), Backend())
    with pytest.raises(ValueError):
        rollout.plan(candidate(**changes), [worker()])


def test_plan_is_durable_idempotent_and_rejects_conflicts(tmp_path):
    store = RolloutStore(tmp_path / "r.json")
    rollout = WorkerRollout(store, Backend())
    first = rollout.plan(candidate(), [worker()], operation_id="rollout-1", aggregate_budget=80)
    assert rollout.plan(candidate(), [worker(name="ignored")]) == first
    assert first["workers"][0]["target"]["deadline"] == "2026-10-01T00:00:00Z"
    with pytest.raises(ValueError, match="conflicting"):
        rollout.plan(candidate(version="1.2.4"), [worker()])


def test_success_requires_repeated_protocol_health_and_exact_receipt(tmp_path):
    rollout, backend = operation(tmp_path)
    result = rollout.start()
    assert result["status"] == "complete"
    assert backend.health_calls == 3
    assert result["workers"][0]["state"] == "complete"
    states = [row["state"] for row in result["workers"][0]["transitions"]]
    assert states == ["planned", "waiting-for-idle", "draining", "staged", "verified",
                      "activated", "health-checking", "health-checking", "health-checking",
                      "health-checking", "complete"]


def test_completed_worker_is_not_reinstalled_on_resume(tmp_path):
    rollout, backend = operation(tmp_path)
    rollout.start()
    assert rollout.resume()["status"] == "complete"
    assert backend.stages == 1 and backend.activations == 1


def test_interrupted_operation_resumes_from_durable_transition_log(tmp_path):
    rollout, backend = operation(tmp_path)
    original = backend.stage

    def interrupted(target, release):
        backend.stage = original
        raise RuntimeError("controller interrupted")

    backend.stage = interrupted
    with pytest.raises(RuntimeError, match="interrupted"):
        rollout.start()
    assert rollout.status()["workers"][0]["state"] == "draining"
    assert rollout.resume()["status"] == "complete"


def test_resume_after_activation_does_not_repeat_switch(tmp_path):
    rollout, backend = operation(tmp_path)
    original = backend.health

    def interrupted(target, release):
        backend.health = original
        raise RuntimeError("controller interrupted after activation")

    backend.health = interrupted
    with pytest.raises(RuntimeError, match="after activation"):
        rollout.start()
    assert rollout.status()["workers"][0]["state"] == "health-checking"

    assert rollout.resume()["status"] == "complete"
    assert backend.stages == 1
    assert backend.activations == 1


def test_resume_during_health_keeps_durable_samples(tmp_path):
    rollout, backend = operation(tmp_path)
    original = backend.health

    def interrupted(target, release):
        if backend.health_calls == 1:
            backend.health = original
            raise RuntimeError("controller interrupted during health checking")
        return original(target, release)

    backend.health = interrupted
    with pytest.raises(RuntimeError, match="during health checking"):
        rollout.start()
    record = rollout.status()["workers"][0]
    assert record["state"] == "health-checking"
    assert len(record["transitions"][-1]["health"]) == 1

    assert rollout.resume()["status"] == "complete"
    assert backend.activations == 1
    assert backend.health_calls == 3


def test_store_fsyncs_file_and_directory(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("garden.worker_rollout.os.fsync", calls.append)

    rollout, _ = operation(tmp_path)

    assert rollout.status()["status"] == "planned"
    assert len(calls) == 2


@pytest.mark.parametrize(("where", "change"), [
    ("stage", {"manifest": {"garden.py": "c" * 64}}),
    ("active", {"prior_pid": 22}),
    ("active", {"owner": "root"}),
    ("active", {"unit_mode": "0666"}),
    ("active", {"source_commit": "c" * 40}),
    ("active", {"frozen": True}),
    ("active", {"ready": False}),
])
def test_exact_staging_and_service_attestations_fail_closed(tmp_path, where, change):
    rollout, backend = operation(tmp_path)
    setattr(backend, f"{where}_changes", change)
    result = rollout.start()
    assert result["status"] == "failed"
    expected = "failed" if where == "stage" else "rolled-back"
    assert result["workers"][0]["state"] == expected


def test_restart_loop_after_initial_success_fails_and_rolls_back(tmp_path):
    rollout, backend = operation(tmp_path)
    original = backend.health

    def health(target, release):
        result = original(target, release)
        if backend.health_calls == 2:
            result["restarted"] = True
        return result

    backend.health = health
    assert rollout.start()["workers"][0]["state"] == "rolled-back"


def test_claim_race_defers_without_activation(tmp_path):
    rollout, backend = operation(tmp_path)
    observations = 0

    def race(value):
        nonlocal observations
        observations += 1
        if observations == 2:  # initial idle, then fresh activation boundary
            value.generation = "claim-2"
            value.busy = True

    backend.observe_hook = race
    result = rollout.start()
    assert result["workers"][0]["state"] == "deferred"
    assert backend.activations == 0


def test_claim_winning_inside_activation_is_deferred(tmp_path):
    rollout, backend = operation(tmp_path)
    backend.active_changes = {"busy": True}
    result = rollout.start()
    assert result["workers"][0]["state"] == "deferred"


def test_partial_fleet_failure_stops_later_worker(tmp_path):
    backend = Backend()
    rollout = WorkerRollout(RolloutStore(tmp_path / "r.json"), backend, sleep=lambda _: None)
    rollout.plan(candidate(), [worker(), worker("worker-2", host_id="host-2")])
    backend.stage_changes = {"version": "wrong"}
    result = rollout.start()
    assert [record["state"] for record in result["workers"]] == ["failed", "planned"]
    assert backend.stages == 1


def test_rollback_failure_leaves_worker_fenced_with_recovery(tmp_path):
    rollout, backend = operation(tmp_path)
    backend.active_changes = {"ready": False}
    backend.rollback_ok = False
    result = rollout.start()
    record = result["workers"][0]
    assert record["state"] == "failed"
    assert "repair retained runtime" in record["recovery"]
    assert backend.fenced


def test_abort_is_durable_and_prevents_mutation(tmp_path):
    rollout, backend = operation(tmp_path)
    assert rollout.abort()["status"] == "aborted"
    assert rollout.resume()["status"] == "aborted"
    assert backend.stages == 0


def test_disposable_command_backend_drives_bounded_protocol_journey(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text("""
import json, sys
r = json.load(sys.stdin); w = r["worker"]; c = r.get("candidate", {}); action = r["action"]
responses = {
 "observe": {"busy": False, "pending_collection": False, "claim_generation": "idle-1", "runtime": "/old"},
 "stage": {"runtime": "/new", "version": c.get("version"), "direct_url_commit": c.get("source_commit"), "manifest": c.get("manifest"), "executable": "/new/garden", "tools_ok": True},
 "activate": {"runtime": "/new", "version": c.get("version"), "source_commit": c.get("source_commit"), "direct_url_commit": c.get("source_commit"), "manifest": c.get("manifest"), "config_path": w["config_path"], "owner": w["owner"], "group": w["group"], "unit_mode": w["unit_mode"], "unit_source": w["unit_source"], "pid": 22, "prior_pid": 11, "frozen": False, "ready": True, "executable": "/new/garden", "runtime_executable": "/new/garden"},
 "health": {"authenticated": True, "claim_probe": True, "heartbeat_probe": True, "finish_probe": True, "ready": True, "restarted": False, "resource_failure": False, "version": c.get("version"), "source_commit": c.get("source_commit")}}
json.dump(responses[action], sys.stdout)
""")
    backend = CommandWorkerRolloutBackend([sys.executable, str(adapter)], timeout=2)
    rollout = WorkerRollout(RolloutStore(tmp_path / "journey.json"), backend,
                            health_samples=2, health_interval=0, sleep=lambda _: None)
    rollout.plan(candidate(), [worker()])
    assert rollout.start()["status"] == "complete"
