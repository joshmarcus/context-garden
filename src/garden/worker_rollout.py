"""Durable, fail-closed rollout of an immutable release to existing workers.

The orchestrator deliberately knows nothing about SSH, systemd, or a cloud provider.  A
``WorkerRolloutBackend`` performs those host-local operations and returns attestations; this
module owns identity validation, ordering, durable receipts, drain races, and rollback rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .locking import file_lock

ROLLOUT_CONTRACT = "garden.worker-rollout/v1"
TERMINAL = {"complete", "deferred", "failed", "rolled-back"}


@dataclass(frozen=True)
class PublishedVersion:
    version: str
    source_commit: str
    manifest: Mapping[str, str]
    verified: bool

    def validate(self) -> None:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.-]+)?", self.version):
            raise ValueError("published version must be an immutable version number")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise ValueError("source commit must be a full lowercase commit object ID")
        if not self.verified or not self.manifest:
            raise ValueError("a verified, non-empty source manifest is required")
        for path, digest in self.manifest.items():
            if not path or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("source manifest requires paths with full sha256 digests")

    @property
    def identity(self) -> str:
        value = {"version": self.version, "source_commit": self.source_commit,
                 "manifest": dict(sorted(self.manifest.items()))}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class WorkerTarget:
    worker_id: str
    host_id: str
    owner: str
    group: str
    config_path: str
    unit_source: str
    unit_mode: str
    resource_caps: Mapping[str, Any]
    deadline: str
    enrollment_boundary: str
    secret_boundary: str


class WorkerRolloutBackend(Protocol):
    def observe(self, worker: WorkerTarget) -> Mapping[str, Any]: ...
    def stage(self, worker: WorkerTarget, candidate: PublishedVersion) -> Mapping[str, Any]: ...
    def activate(self, worker: WorkerTarget, candidate: PublishedVersion,
                 idle_generation: str) -> Mapping[str, Any]: ...
    def health(self, worker: WorkerTarget, candidate: PublishedVersion) -> Mapping[str, Any]: ...
    def rollback(self, worker: WorkerTarget, prior_runtime: str,
                 idle_generation: str) -> Mapping[str, Any]: ...
    def fence(self, worker: WorkerTarget, reason: str) -> None: ...


class CommandWorkerRolloutBackend:
    """Bounded JSON-over-stdio boundary for approved host control tooling."""

    def __init__(self, command: Sequence[str], *, timeout: float = 120,
                 env: Mapping[str, str] | None = None):
        if not command or timeout <= 0:
            raise ValueError("rollout backend requires a command and positive timeout")
        self.command, self.timeout = tuple(command), timeout
        self.env = dict(env) if env is not None else None

    def _call(self, action: str, worker: WorkerTarget, **values: Any) -> dict[str, Any]:
        request = {"contract": ROLLOUT_CONTRACT, "action": action,
                   "worker": {**asdict(worker), "resource_caps": dict(worker.resource_caps)},
                   **values}
        try:
            result = subprocess.run(self.command, input=json.dumps(request), text=True,
                                    capture_output=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"worker {worker.worker_id} {action} timed out") from exc
        if result.returncode:
            raise RuntimeError(f"worker {worker.worker_id} {action} failed with exit "
                               f"{result.returncode}: {result.stderr.strip()[-1000:]}")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"worker {worker.worker_id} {action} returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RuntimeError(f"worker {worker.worker_id} {action} returned a non-object response")
        return response

    @staticmethod
    def _release(candidate: PublishedVersion) -> dict[str, Any]:
        return {**asdict(candidate), "manifest": dict(candidate.manifest)}

    def observe(self, worker: WorkerTarget) -> Mapping[str, Any]:
        return self._call("observe", worker)

    def stage(self, worker: WorkerTarget, candidate: PublishedVersion) -> Mapping[str, Any]:
        return self._call("stage", worker, candidate=self._release(candidate))

    def activate(self, worker: WorkerTarget, candidate: PublishedVersion,
                 idle_generation: str) -> Mapping[str, Any]:
        return self._call("activate", worker, candidate=self._release(candidate),
                          idle_generation=idle_generation)

    def health(self, worker: WorkerTarget, candidate: PublishedVersion) -> Mapping[str, Any]:
        return self._call("health", worker, candidate=self._release(candidate))

    def rollback(self, worker: WorkerTarget, prior_runtime: str,
                 idle_generation: str) -> Mapping[str, Any]:
        return self._call("rollback", worker, prior_runtime=prior_runtime,
                          idle_generation=idle_generation)

    def fence(self, worker: WorkerTarget, reason: str) -> None:
        if not self._call("fence", worker, reason=reason).get("ok"):
            raise RuntimeError(f"worker {worker.worker_id} could not be fenced")


class RolloutStore:
    """Atomic JSON journal. Each transition is flushed before the next mutation."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            yield

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("contract") != ROLLOUT_CONTRACT:
            raise ValueError("rollout journal is corrupt or has an unsupported contract")
        return value

    def write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        with temporary.open("w") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Some supported filesystems cannot fsync directories. The atomically
            # replaced, fsynced file is still the strongest portable guarantee.
            pass


class WorkerRollout:
    def __init__(self, store: RolloutStore, backend: WorkerRolloutBackend, *,
                 now: Callable[[], float] = time.time, health_samples: int = 3,
                 health_interval: float = 5.0, sleep: Callable[[float], None] = time.sleep):
        if health_samples < 2 or health_interval < 0:
            raise ValueError("stable health requires at least two bounded samples")
        self.store, self.backend, self.now = store, backend, now
        self.health_samples, self.health_interval, self.sleep = health_samples, health_interval, sleep

    def _at(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now()))

    def plan(self, candidate: PublishedVersion, workers: Sequence[WorkerTarget], *,
             operation_id: str = "", aggregate_budget: float | None = None,
             controller_runtime: str = "") -> dict[str, Any]:
        candidate.validate()
        if not workers or len({w.worker_id for w in workers}) != len(workers):
            raise ValueError("configured worker inventory must be non-empty and unique")
        identity = candidate.identity
        with self.store.locked():
            existing = self.store.read()
            if existing:
                if existing["candidate"]["identity"] != identity:
                    raise ValueError("conflicting rollout identity already exists")
                return existing
            at = self._at()
            state = {
                "contract": ROLLOUT_CONTRACT, "operation_id": operation_id or str(uuid.uuid4()),
                "status": "planned", "created_at": at, "updated_at": at,
                "candidate": {**asdict(candidate), "manifest": dict(candidate.manifest),
                              "identity": identity},
                "invariants": {"aggregate_budget": aggregate_budget,
                               "controller_runtime": controller_runtime},
                "workers": [], "events": [],
            }
            for worker in workers:
                observed = dict(self.backend.observe(worker))
                state["workers"].append({"target": {**asdict(worker),
                    "resource_caps": dict(worker.resource_caps)}, "state": "planned",
                    "at": at, "prior_runtime": str(observed.get("runtime") or ""),
                    "transitions": [{"state": "planned", "at": at}]})
            self.store.write(state)
            return state

    def status(self) -> dict[str, Any]:
        state = self.store.read()
        if not state:
            raise ValueError("rollout has not been planned")
        return state

    def abort(self) -> dict[str, Any]:
        with self.store.locked():
            state = self.status()
            if state["status"] == "complete":
                raise ValueError("a completed rollout cannot be aborted")
            state["status"], state["updated_at"] = "aborted", self._at()
            self.store.write(state)
            return state

    def start(self) -> dict[str, Any]:
        return self.resume()

    def resume(self) -> dict[str, Any]:
        with self.store.locked():
            state = self.status()
            if state["status"] == "aborted":
                return state
            candidate = PublishedVersion(**{key: state["candidate"][key]
                for key in ("version", "source_commit", "manifest", "verified")})
            for record in state["workers"]:
                if record["state"] == "complete":
                    continue
                if record["state"] in {"failed", "rolled-back"}:
                    state["status"] = "failed"
                    break
                if not self._advance(state, record, candidate):
                    break
            else:
                state["status"] = "complete"
            state["updated_at"] = self._at()
            self.store.write(state)
            return state

    def _transition(self, state: dict[str, Any], record: dict[str, Any], name: str,
                    **evidence: Any) -> None:
        at = self._at()
        record.update(state=name, at=at)
        transition = {"state": name, "at": at, **evidence}
        record["transitions"].append(transition)
        state["events"].append({"worker_id": record["target"]["worker_id"], **transition})
        state["updated_at"] = at
        self.store.write(state)

    @staticmethod
    def _target(record: Mapping[str, Any]) -> WorkerTarget:
        return WorkerTarget(**record["target"])

    def _advance(self, state: dict[str, Any], record: dict[str, Any],
                 candidate: PublishedVersion) -> bool:
        worker = self._target(record)
        phase = record["state"]
        if phase in {"planned", "waiting-for-idle", "draining", "deferred"}:
            observation = dict(self.backend.observe(worker))
            self._transition(state, record, "waiting-for-idle", observation=observation)
            if observation.get("busy") or observation.get("pending_collection"):
                self._transition(
                    state, record, "deferred",
                    reason="worker is busy or has pending collection",
                )
                return False
            generation = str(observation.get("claim_generation") or "")
            if not generation:
                return self._fail(state, record, worker, "idle observation lacks claim generation")
            self._transition(state, record, "draining", idle_generation=generation)
            staged = dict(self.backend.stage(worker, candidate))
            self._transition(state, record, "staged", staging=staged)
        else:
            staged = self._evidence(record, "staged", "staging")

        error = self._verify_staged(staged, candidate)
        if error:
            return self._fail(state, record, worker, error)
        if phase == "staged" or record["state"] == "staged":
            self._transition(state, record, "verified", verification=staged)

        if record["state"] == "verified":
            fresh = dict(self.backend.observe(worker))
            generation = str(self._evidence(record, "draining", "idle_generation"))
            if (fresh.get("busy") or fresh.get("pending_collection")
                    or fresh.get("claim_generation") != generation):
                self._transition(state, record, "deferred",
                                 reason="claim changed at activation boundary", observation=fresh)
                return False
            activated = dict(self.backend.activate(worker, candidate, generation))
            if activated.get("busy"):
                self._transition(state, record, "deferred", reason="claim won activation race")
                return False
            self._transition(state, record, "activated", activation=activated)
        else:
            activated = self._evidence(record, "activated", "activation")

        error = self._verify_active(activated, worker, candidate)
        if error:
            return self._fail(state, record, worker, error)
        if record["state"] == "activated":
            self._transition(state, record, "health-checking", health=[])
        samples = list(self._evidence(record, "health-checking", "health", default=[]))
        for index in range(len(samples), self.health_samples):
            if index:
                self.sleep(self.health_interval)
            sample = dict(self.backend.health(worker, candidate))
            samples.append(sample)
            self._transition(state, record, "health-checking", health=list(samples))
            if not self._healthy(sample, candidate):
                return self._fail(state, record, worker, "live health stability check failed",
                                  health=samples)
        self._transition(state, record, "complete", health=samples,
                         version=candidate.version, source_commit=candidate.source_commit)
        return True

    @staticmethod
    def _evidence(record: Mapping[str, Any], state: str, key: str, *, default: Any = None) -> Any:
        for transition in reversed(record["transitions"]):
            if transition["state"] == state and key in transition:
                return transition[key]
        return default

    @staticmethod
    def _verify_staged(facts: Mapping[str, Any], candidate: PublishedVersion) -> str:
        required = ("runtime", "version", "direct_url_commit", "manifest", "executable",
                    "tools_ok")
        if any(not facts.get(key) for key in required):
            return "staged runtime attestation is incomplete"
        if (facts["version"] != candidate.version or facts["direct_url_commit"] != candidate.source_commit
                or facts["manifest"] != dict(candidate.manifest)):
            return "staged package or source identity does not match candidate"
        return ""

    @staticmethod
    def _verify_active(facts: Mapping[str, Any], worker: WorkerTarget,
                       candidate: PublishedVersion) -> str:
        expected = {"version": candidate.version, "source_commit": candidate.source_commit,
                    "direct_url_commit": candidate.source_commit,
                    "manifest": dict(candidate.manifest),
                    "config_path": worker.config_path, "owner": worker.owner, "group": worker.group,
                    "unit_mode": worker.unit_mode, "unit_source": worker.unit_source}
        if any(facts.get(key) != value for key, value in expected.items()):
            return "active service identity or configuration does not match plan"
        if (not facts.get("runtime") or not facts.get("pid")
                or facts.get("pid") == facts.get("prior_pid")
                or facts.get("frozen") or not facts.get("ready")
                or facts.get("executable") != facts.get("runtime_executable")):
            return "active service PID, executable, freeze state, or readiness is invalid"
        return ""

    @staticmethod
    def _healthy(sample: Mapping[str, Any], candidate: PublishedVersion) -> bool:
        return bool(sample.get("authenticated") and sample.get("claim_probe")
                    and sample.get("heartbeat_probe") and sample.get("finish_probe")
                    and sample.get("ready") and not sample.get("restarted")
                    and not sample.get("resource_failure")
                    and sample.get("version") == candidate.version
                    and sample.get("source_commit") == candidate.source_commit)

    def _fail(self, state: dict[str, Any], record: dict[str, Any], worker: WorkerTarget,
              reason: str, **evidence: Any) -> bool:
        self._transition(state, record, "failed", reason=reason, **evidence)
        activated = any(row["state"] == "activated" for row in record["transitions"])
        if not activated:
            record["recovery"] = "activation did not occur; retained prior runtime remains active"
            state["status"] = "failed"
            self.store.write(state)
            return False
        fresh = dict(self.backend.observe(worker))
        generation = str(fresh.get("claim_generation") or "")
        prior = str(record.get("prior_runtime") or "")
        if fresh.get("busy") or fresh.get("pending_collection"):
            self.backend.fence(worker, reason)
            record["recovery"] = "worker became busy; leave fenced and collect work before recovery"
        elif prior and generation:
            result = dict(self.backend.rollback(worker, prior, generation))
            if result.get("ok") and result.get("runtime") == prior and result.get("ready"):
                self._transition(state, record, "rolled-back", rollback=result)
            else:
                self.backend.fence(worker, reason)
                record["recovery"] = f"rollback failed; repair retained runtime {prior} and verify before thaw"
                self.store.write(state)
        else:
            self.backend.fence(worker, reason)
            record["recovery"] = "no verified prior runtime or idle generation; repair while fenced"
            self.store.write(state)
        state["status"] = "failed"
        return False
