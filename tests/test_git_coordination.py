from __future__ import annotations

import multiprocessing
import subprocess
from pathlib import Path

import pytest

from garden.config import Config
from garden.git_coordination import GitContention, GitCoordinationError, GitStateStore


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, check=True, capture_output=True)
    return result.stdout.strip()


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    paths = []
    for name in ("one", "two"):
        path = tmp_path / name
        subprocess.run(
            ["git", "clone", str(remote), str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        git(path, "config", "user.name", name)
        git(path, "config", "user.email", f"{name}@example.test")
        paths.append(path)
    GitStateStore.initialize(paths[0], garden_id="garden")
    seed = GitStateStore(paths[0], garden_id="garden")
    seed.transact(
        "enroll",
        {"actor": "admin", "installation": "one"},
        lambda state: (
            state["members"].update(
                {
                    "admin": {"active": True, "role": "administrator"},
                    "alice": {"active": True},
                    "bob": {"active": True},
                }
            ),
            state["installations"].update({"one": "alice", "two": "alice", "bob": "bob"}),
            state["policy"]["pools"].update({"workers": {"units": 1, "spend_micros": 10}}),
            state["entities"].update(
                {"task:CG-1": {"kind": "task", "scope": "CG-1", "owner": "alice", "version": 0}}
            ),
            {},
        )[-1],
    )
    return remote, paths[0], paths[1]


def test_atomic_claim_permit_and_reservation_and_owned_release(clones):
    _, one, two = clones
    first = GitStateStore(one, garden_id="garden")
    accepted = first.apply(
        "launch",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={
            "claims": {"task:CG-1": {"generation": 1}},
            "permits": {"run:1": {"claim": "task:CG-1"}},
            "reservations": {"workers:1": {"pool": "workers", "units": 1, "spend_micros": 10}},
        },
    )
    assert accepted.result["sequence"] == 2
    replay = GitStateStore(two, garden_id="garden").apply(
        "launch",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={
            "claims": {"task:CG-1": {"generation": 1}},
            "permits": {"run:1": {"claim": "task:CG-1"}},
            "reservations": {"workers:1": {"pool": "workers", "units": 1, "spend_micros": 10}},
        },
    )
    assert replay.replayed
    with pytest.raises(GitCoordinationError, match="different inputs"):
        first.transact("launch", {"changed": True}, lambda state: {})
    with pytest.raises(PermissionError):
        first.apply(
            "steal-release",
            actor="bob",
            installation="bob",
            expected_versions={},
            changes={"reservations": {"workers:1": None}},
        )
    first.apply(
        "release",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={"reservations": {"workers:1": None}, "permits": {"run:1": None}},
    )
    assert not first.read()[1]["reservations"]
    first.apply(
        "reacquire",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={
            "reservations": {
                "workers:2": {"pool": "workers", "units": 1, "spend_micros": 10}
            }
        },
    )
    assert "workers:2" in first.read()[1]["reservations"]


def _compete(repo: str, installation: str, output: multiprocessing.Queue) -> None:
    store = GitStateStore(Path(repo), garden_id="garden", retries=4)
    try:
        result = store.apply(
            f"claim-{installation}",
            actor="alice",
            installation=installation,
            expected_versions={"task:CG-1": 0},
            changes={"claims": {"task:CG-1": {"generation": 1}}},
        )
        output.put((installation, "accepted", result.commit))
    except (GitContention, PermissionError) as exc:
        output.put((installation, "rejected", str(exc)))


def _reserve(repo: str, installation: str, output: multiprocessing.Queue) -> None:
    store = GitStateStore(Path(repo), garden_id="garden", retries=4)
    try:
        result = store.apply(
            f"reserve-{installation}",
            actor="alice",
            installation=installation,
            expected_versions={},
            changes={
                "reservations": {
                    f"workers:{installation}": {"pool": "workers", "units": 1, "spend_micros": 10}
                }
            },
        )
        output.put((installation, "accepted", result.commit))
    except GitContention as exc:
        output.put((installation, "rejected", str(exc)))


def test_independent_processes_contend_over_actual_remote(clones):
    _, one, two = clones
    queue: multiprocessing.Queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_compete, args=(str(repo), installation, queue))
        for repo, installation in ((one, "one"), (two, "two"))
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    results = [queue.get(timeout=2), queue.get(timeout=2)]
    assert sorted(result[1] for result in results) == ["accepted", "rejected"]


def test_independent_processes_share_authoritative_budget(clones):
    _, one, two = clones
    queue: multiprocessing.Queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_reserve, args=(str(repo), installation, queue))
        for repo, installation in ((one, "one"), (two, "two"))
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2)[1] for _ in processes) == ["accepted", "rejected"]


def test_missing_or_rewritten_ref_fails_closed_with_evidence(clones):
    remote, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    head, _ = store.read()
    git(remote, "update-ref", "-d", "refs/heads/garden-state", head)
    with pytest.raises(GitCoordinationError, match="missing"):
        store.read()
    assert list(store.evidence_dir.glob("*.json"))


def test_lost_push_acknowledgement_resolves_operation_from_remote(clones, monkeypatch):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    actual_push = store._push

    def lose_ack(commit: str, expected: str) -> None:
        actual_push(commit, expected)
        raise GitCoordinationError("connection closed after receive")

    monkeypatch.setattr(store, "_push", lose_ack)
    accepted = store.apply(
        "lost-ack",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={
            "reservations": {"workers:lost": {"pool": "workers", "units": 1, "spend_micros": 10}}
        },
    )
    assert accepted.replayed
    assert GitStateStore(one, garden_id="garden").read()[1]["operations"]["lost-ack"]


@pytest.mark.parametrize("enabled", ["false", 1, []])
def test_multiplayer_enabled_is_strict_boolean(tmp_path: Path, enabled):
    (tmp_path / "garden.yaml").write_text(f"multiplayer:\n  enabled: {enabled!r}\n")
    with pytest.raises(ValueError, match="must be true or false"):
        Config.load(tmp_path)
