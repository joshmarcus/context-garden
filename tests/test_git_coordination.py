from __future__ import annotations

import multiprocessing
import subprocess
from pathlib import Path

import pytest

from garden.config import Config
from garden.git_coordination import (
    GitContention,
    GitCoordinationError,
    GitMultiplayerClient,
    GitStateStore,
)


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
                {
                    "task:CG-1": {
                        "kind": "task",
                        "scope": "CG-1",
                        "owner": "alice",
                        "authority_generation": 1,
                        "version": 0,
                    }
                }
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
            "claims": {"task:CG-1": {
                "kind": "task", "scope": "CG-1", "owner_id": "alice",
                "authority_generation": 1, "operation_id": "claim-one",
            }},
            "permits": {"run:1": {"claim": "claim-one"}},
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
            "claims": {"task:CG-1": {
                "kind": "task", "scope": "CG-1", "owner_id": "alice",
                "authority_generation": 1, "operation_id": "claim-one",
            }},
            "permits": {"run:1": {"claim": "claim-one"}},
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
            changes={"claims": {"task:CG-1": {
                "kind": "task", "scope": "CG-1", "owner_id": "alice",
                "authority_generation": 1, "operation_id": f"claim-{installation}",
            }}},
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


def test_effect_replay_never_grants_execution_twice(clones):
    _, one, _ = clones
    client = GitMultiplayerClient(
        GitStateStore(one, garden_id="garden"), "alice", "one"
    )
    executions = 0

    with client.effect(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0, effect_key="publish",
    ):
        executions += 1

    with pytest.raises(GitCoordinationError, match="reconcile it instead of executing again"):
        with client.effect(
            kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
            expected_version=0, effect_key="publish",
        ):
            executions += 1

    assert executions == 1
    state = GitStateStore(one, garden_id="garden").read()[1]
    effect = state["effects"]["effect:one:publish:1"]
    assert effect["outcome"] == "succeeded"
    client.store.apply(
        "handoff-after-terminal-effect",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={"entities": {"task:CG-1": {
            "owner": "bob", "authority_generation": 2,
        }}},
    )
    assert client.store.read()[1]["entities"]["task:CG-1"]["owner"] == "bob"


@pytest.mark.parametrize(
    ("owner_id", "generation", "error", "message"),
    [
        ("bob", 1, PermissionError, "owner"),
        ("alice", 99, GitCoordinationError, "generation"),
    ],
)
def test_claim_must_match_authoritative_owner_and_generation(
    clones, owner_id, generation, error, message
):
    _, one, _ = clones
    client = GitMultiplayerClient(
        GitStateStore(one, garden_id="garden"), "alice", "one"
    )

    with pytest.raises(error, match=message):
        client.claim(
            kind="task", scope="CG-1", owner_id=owner_id,
            authority_generation=generation, expected_version=0,
        )


def test_same_member_installation_cannot_release_another_installations_claim(clones):
    _, one, two = clones
    owner = GitMultiplayerClient(
        GitStateStore(one, garden_id="garden"), "alice", "one"
    )
    owner.claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )

    with pytest.raises(PermissionError, match="cannot release"):
        GitStateStore(two, garden_id="garden").apply(
            "release-from-other-installation",
            actor="alice",
            installation="two",
            expected_versions={},
            changes={"claims": {"task:CG-1": None}},
        )


def test_pending_execution_permit_blocks_owner_handoff(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    client = GitMultiplayerClient(store, "alice", "one")
    claim = client.claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.apply(
        "admit-before-launch",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={"permits": {"run:pending": {"claim": claim["operation_id"]}}},
    )

    with pytest.raises(GitCoordinationError, match="unresolved permits or effects"):
        store.apply(
            "handoff",
            actor="alice",
            installation="one",
            expected_versions={"task:CG-1": 0},
            changes={"entities": {"task:CG-1": {
                "owner": "bob", "authority_generation": 2,
            }}},
        )

    state = store.read()[1]
    assert state["entities"]["task:CG-1"]["owner"] == "alice"
    assert "run:pending" in state["permits"]


def test_pending_execution_permit_survives_claim_release_handoff_attempts(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    client = GitMultiplayerClient(store, "alice", "one")
    claim = client.claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.apply(
        "admit-before-release",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={"permits": {"run:pending": {"claim": claim["operation_id"]}}},
    )

    for operation, changes in (
        ("release-claim", {"claims": {"task:CG-1": None}}),
        ("release-and-handoff", {
            "claims": {"task:CG-1": None},
            "entities": {"task:CG-1": {"owner": "bob", "authority_generation": 2}},
        }),
    ):
        with pytest.raises(GitCoordinationError, match="unresolved permits or effects"):
            store.apply(
                operation,
                actor="alice",
                installation="one",
                expected_versions={"task:CG-1": 0},
                changes=changes,
            )

    state = store.read()[1]
    assert state["claims"]["task:CG-1"]["operation_id"] == claim["operation_id"]
    assert state["entities"]["task:CG-1"]["owner"] == "alice"

    store.apply(
        "acknowledge-terminal",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={"effects": {"run:pending": {
            "claim": claim["operation_id"],
            "provider": "scheduler",
            "scope": "task:CG-1",
            "effect_key": "run:pending",
            "outcome": "succeeded",
        }}},
    )
    store.apply(
        "release-and-handoff-after-terminal",
        actor="alice",
        installation="one",
        expected_versions={"task:CG-1": 0},
        changes={
            "claims": {"task:CG-1": None},
            "entities": {"task:CG-1": {"owner": "bob", "authority_generation": 2}},
        },
    )
    state = store.read()[1]
    assert "task:CG-1" not in state["claims"]
    assert state["entities"]["task:CG-1"]["owner"] == "bob"


@pytest.mark.parametrize("enabled", ["false", 1, []])
def test_multiplayer_enabled_is_strict_boolean(tmp_path: Path, enabled):
    (tmp_path / "garden.yaml").write_text(f"multiplayer:\n  enabled: {enabled!r}\n")
    with pytest.raises(ValueError, match="must be true or false"):
        Config.load(tmp_path)
