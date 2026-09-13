from __future__ import annotations

import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden.cli import app as cli_app
from garden.config import Config
from garden.git_coordination import (
    GitContention,
    GitCoordinationError,
    GitMultiplayerClient,
    GitStateStore,
)
from garden.multiplayer_client import MultiplayerUnavailable, ProjectionConflict


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
                    "alice": {
                        "active": True, "role": "member", "project_visibility": "all",
                        "assignment": {
                            "member_id": "alice", "project": "demo", "phase": "p1",
                            "enabled": True, "generation": 1,
                        },
                    },
                    "bob": {"active": True, "role": "member", "project_visibility": "all"},
                }
            ),
            state["installations"].update(
                {"admin": "admin", "one": "alice", "two": "alice", "bob": "bob"}
            ),
            state["policy"]["pools"].update({"workers": {"units": 1, "spend_micros": 10}}),
            state["entities"].update(
                {
                    "task:CG-1": {
                        "kind": "task",
                        "project": "demo",
                        "scope": "CG-1",
                        "owner": "alice",
                        "authority_generation": 1,
                        "version": 0,
                    },
                    "task:CG-2": {
                        "kind": "task", "project": "demo", "scope": "CG-2", "owner": "-",
                        "authority_generation": 1, "version": 0,
                    },
                    "phase:demo/p1": {
                        "kind": "phase", "project": "demo", "scope": "demo/p1", "owner": "alice",
                        "authority_generation": 1, "version": 0,
                    },
                }
            ),
            {},
        )[-1],
    )
    return remote, paths[0], paths[1]


def test_client_starts_phase_handoff_from_one_assignment_intent(clones):
    _, one, _ = clones
    client = GitMultiplayerClient(GitStateStore(one, garden_id="garden"), "admin", "admin")

    result = client.begin_handoff(
        kind="phase", scope="demo/p1", pending_owner="bob", expected_version=0,
    )

    assert result == {"status": "complete", "owner": "bob", "authority_generation": 2}
    snapshot = client.refresh(allow_stale=False).snapshot
    assert "phase:demo/p1" not in snapshot["handoffs"]
    assert snapshot["entities"]["phase:demo/p1"]["owner"] == "bob"


def test_client_automatically_stops_and_completes_claimed_handoff(clones):
    _, one, _ = clones
    client = GitMultiplayerClient(GitStateStore(one, garden_id="garden"), "alice", "one")
    client.claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    client.begin_handoff(
        kind="task", scope="CG-1", pending_owner="bob", expected_version=0,
    )

    snapshot = client.refresh(allow_stale=False).snapshot
    assert client.acknowledge_cancellations(snapshot, lambda kind, scope: True) == ["task:CG-1"]
    final = client.refresh(allow_stale=False).snapshot
    assert final["entities"]["task:CG-1"]["owner"] == "bob"
    assert "task:CG-1" not in final["handoffs"]


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
        "record-terminal-effect",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={"effects": {"run:1": {
            "claim": "claim-one", "provider": "scheduler", "scope": "task:CG-1",
            "effect_key": "run:1", "outcome": "succeeded",
        }}},
    )
    first.apply(
        "release",
        actor="alice",
        installation="one",
        expected_versions={},
        changes={"reservations": {"workers:1": None}},
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
    client.store.begin_handoff(
        "drain-after-terminal-effect", actor="alice", installation="one",
        entity_key="task:CG-1", expected_version=0, pending_owner="bob",
    )
    client.store.acknowledge_stop(
        "stop-after-terminal-effect", actor="alice", installation="one",
        entity_key="task:CG-1",
    )
    client.store.complete_handoff(
        "handoff-after-terminal-effect", actor="bob", installation="bob",
        entity_key="task:CG-1",
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

    store.begin_handoff(
        "begin-pending-handoff", actor="alice", installation="one",
        entity_key="task:CG-1", expected_version=0, pending_owner="bob",
    )
    with pytest.raises(GitCoordinationError, match="run:pending"):
        store.acknowledge_stop(
            "ack-pending-handoff", actor="alice", installation="one",
            entity_key="task:CG-1",
        )

    state = store.read()[1]
    assert state["entities"]["task:CG-1"]["owner"] == "alice"
    assert "run:pending" in state["permits"]


def test_inherited_task_owner_stays_accepted_until_git_handoff_completes(clones):
    """A changed phase default cannot transfer an active inherited task implicitly."""
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    alice = GitMultiplayerClient(store, "alice", "one")
    claim = alice.claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.apply(
        "active-inherited-run", actor="alice", installation="one",
        expected_versions={"task:CG-1": 0},
        changes={
            "permits": {"run:inherited": {"claim": claim["operation_id"]}},
            "effects": {"run:inherited": {
                "claim": claim["operation_id"], "provider": "scheduler",
                "scope": "task:CG-1", "effect_key": "run:inherited", "outcome": "pending",
            }},
        },
    )

    pending = store.begin_handoff(
        "inherit-phase-owner-bob", actor="admin", installation="admin",
        entity_key="task:CG-1", expected_version=0, pending_owner="bob",
    )
    assert pending.result["effective_owner"] == "alice"
    assert store.read()[1]["entities"]["task:CG-1"]["owner"] == "alice"
    with pytest.raises(GitCoordinationError, match="stop acknowledgement blocked"):
        store.acknowledge_stop(
            "early-stop", actor="alice", installation="one", entity_key="task:CG-1",
        )

    store.reconcile_effect(
        "finish-inherited-run", actor="alice", installation="one",
        effect_operation_id="run:inherited", outcome="succeeded",
        evidence={"run": "completed and retained"},
    )
    store.acknowledge_stop(
        "inherited-stopped", actor="alice", installation="one", entity_key="task:CG-1",
    )
    store.complete_handoff(
        "accept-inherited-bob", actor="bob", installation="bob", entity_key="task:CG-1",
    )
    entity = store.read()[1]["entities"]["task:CG-1"]
    assert (entity["owner"], entity["authority_generation"]) == ("bob", 2)


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
@pytest.mark.parametrize(
    "patch",
    [
        {"owner": "bob"},
        {"authority_generation": 2},
        {"scope": "other"},
        {"kind": "invalid"},
        {"draining": True},
        {"pending_owner": "bob"},
    ],
)
def test_generic_apply_cannot_change_handoff_owned_entity_fields(clones, entity_key, patch):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")

    with pytest.raises(GitCoordinationError, match="acknowledged handoff"):
        store.apply(
            f"direct:{entity_key}:{next(iter(patch))}", actor="alice", installation="one",
            expected_versions={entity_key: 0}, changes={"entities": {entity_key: patch}},
        )

    entity = store.read()[1]["entities"][entity_key]
    assert entity["owner"] == "alice"
    assert entity["authority_generation"] == 1
    assert not entity.get("draining", False)


def test_generic_apply_cannot_erase_or_forge_handoff_blockers(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    other_claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="phase", scope="demo/p1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.apply(
        "admit-pending", actor="alice", installation="one", expected_versions={},
        changes={"permits": {"run:pending": {"claim": claim["operation_id"]}}},
    )

    for operation, changes in (
        ("erase-claim", {"claims": {"task:CG-1": None}}),
        ("erase-permit", {"permits": {"run:pending": None}}),
        ("retarget-claim", {"claims": {"task:CG-1": {
            **claim, "operation_id": "claim:forged",
        }}}),
        ("retarget-permit", {"permits": {
            "run:pending": {"claim": other_claim["operation_id"]}
        }}),
        ("forge-existing-fence", {"permits": {
            "run:pending": {"claim": claim["operation_id"], "status": "fenced"}
        }}),
        ("forge-new-fence", {"permits": {
            "run:forged": {"claim": claim["operation_id"], "status": "fenced"}
        }}),
    ):
        with pytest.raises(GitCoordinationError):
            store.apply(
                operation, actor="alice", installation="one",
                expected_versions={}, changes=changes,
            )

    state = store.read()[1]
    assert state["claims"]["task:CG-1"] == claim | {"actor": "alice", "installation": "one"}
    assert state["permits"]["run:pending"].get("status", "pending") == "pending"


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
        with pytest.raises(GitCoordinationError):
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
    store.begin_handoff(
        "begin-after-terminal", actor="alice", installation="one",
        entity_key="task:CG-1", expected_version=0, pending_owner="bob",
    )
    store.acknowledge_stop(
        "ack-after-terminal", actor="alice", installation="one", entity_key="task:CG-1",
    )
    store.complete_handoff(
        "handoff-after-terminal", actor="bob", installation="bob", entity_key="task:CG-1",
    )
    state = store.read()[1]
    assert "task:CG-1" not in state["claims"]
    assert state["entities"]["task:CG-1"]["owner"] == "bob"


@pytest.mark.parametrize("outcome", ["pending", "unknown"])
def test_unresolved_permit_cannot_be_deleted_before_handoff(clones, outcome):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    changes = {"permits": {"run:unresolved": {"claim": claim["operation_id"]}}}
    if outcome == "unknown":
        changes["effects"] = {"run:unresolved": {
            "claim": claim["operation_id"], "provider": "scheduler",
            "scope": "task:CG-1", "effect_key": "run:unresolved", "outcome": "unknown",
        }}
    store.apply(
        "admit-unresolved", actor="alice", installation="one", expected_versions={},
        changes=changes,
    )

    with pytest.raises(GitCoordinationError, match="unresolved permit cannot be released"):
        store.apply(
            "delete-unresolved", actor="alice", installation="one", expected_versions={},
            changes={"permits": {"run:unresolved": None}},
        )
    with pytest.raises(GitCoordinationError, match="unresolved permit cannot be released"):
        store.apply(
            "atomic-delete-and-handoff", actor="alice", installation="one",
            expected_versions={"task:CG-1": 0},
            changes={
                "permits": {"run:unresolved": None},
                "claims": {"task:CG-1": None},
                "entities": {"task:CG-1": {"owner": "bob", "authority_generation": 2}},
            },
        )

    state = store.read()[1]
    assert state["permits"]["run:unresolved"]["claim"] == claim["operation_id"]
    assert state["claims"]["task:CG-1"]["operation_id"] == claim["operation_id"]
    assert state["entities"]["task:CG-1"]["owner"] == "alice"


def test_admitted_records_keep_identity_and_recovery_evidence(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.apply(
        "admit-record", actor="alice", installation="one", expected_versions={},
        changes={
            "permits": {"run:record": {"claim": claim["operation_id"]}},
            "effects": {"run:record": {
                "claim": claim["operation_id"], "provider": "scheduler",
                "scope": "task:CG-1", "effect_key": "run:record", "outcome": "unknown",
            }},
        },
    )

    with pytest.raises(GitCoordinationError, match="permit claim is immutable"):
        store.apply(
            "retarget-permit", actor="alice", installation="one", expected_versions={},
            changes={"permits": {"run:record": {"claim": "different-claim"}}},
        )
    with pytest.raises(GitCoordinationError, match="recovery evidence cannot be deleted"):
        store.apply(
            "delete-effect", actor="alice", installation="one", expected_versions={},
            changes={"effects": {"run:record": None}},
        )


@pytest.mark.parametrize(
    ("kind", "scope", "outcome", "atomic"),
    [
        ("task", "CG-1", "pending", False),
        ("task", "CG-1", "unknown", True),
        ("phase", "demo/p1", "pending", True),
        ("phase", "demo/p1", "unknown", False),
    ],
)
def test_claim_identity_cannot_be_replaced_to_orphan_unresolved_work(
    clones, kind, scope, outcome, atomic
):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    key = f"{kind}:{scope}"
    if kind == "phase":
        store.transact(
            "seed-phase",
            {"actor": "alice", "installation": "one"},
            lambda state: (
                state["entities"].update({key: {
                    "kind": kind, "scope": scope, "owner": "alice",
                    "authority_generation": 1, "version": 0,
                }}),
                {},
            )[-1],
        )
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    effect_key = f"run:{kind}:{outcome}:{atomic}"
    changes = {"permits": {effect_key: {"claim": claim["operation_id"]}}}
    if outcome == "unknown":
        changes["effects"] = {effect_key: {
            "claim": claim["operation_id"], "provider": "scheduler",
            "scope": key, "effect_key": effect_key, "outcome": "unknown",
        }}
    store.apply(
        f"admit:{effect_key}", actor="alice", installation="one",
        expected_versions={}, changes=changes,
    )
    replacement = {**claim, "operation_id": f"replacement:{kind}"}
    replacement_changes = {"claims": {key: replacement}}
    with pytest.raises(GitCoordinationError, match="claim operation_id is immutable"):
        store.apply(
            f"replace:{effect_key}", actor="alice", installation="one",
            expected_versions={key: 0}, changes=replacement_changes,
        )

    with pytest.raises(GitCoordinationError):
        store.apply(
            f"release:{effect_key}", actor="alice", installation="one",
            expected_versions={key: 0}, changes={"claims": {key: None}},
        )
    state = store.read()[1]
    assert state["claims"][key]["operation_id"] == claim["operation_id"]
    assert state["entities"][key]["owner"] == "alice"


def test_claim_identity_requires_complete_authenticated_values(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    valid = {
        "kind": "task", "scope": "CG-1", "owner_id": "alice",
        "authority_generation": 1, "operation_id": "claim-original",
    }
    for index, patch in enumerate((
        {"operation_id": ""},
        {"actor": "bob"},
        {"installation": "two"},
    )):
        with pytest.raises((GitCoordinationError, PermissionError)):
            store.apply(
                f"invalid-claim-{index}", actor="alice", installation="one",
                expected_versions={"task:CG-1": 0},
                changes={"claims": {"task:CG-1": {**valid, **patch}}},
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "claim-replacement"),
        ("kind", "phase"),
        ("scope", "other/p1"),
        ("owner_id", "bob"),
        ("authority_generation", 2),
        ("actor", "bob"),
        ("installation", "two"),
    ],
)
def test_every_admitted_claim_identity_field_is_immutable(clones, field, value):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    _, state = store.read()
    admitted = state["claims"]["task:CG-1"]
    with pytest.raises((GitCoordinationError, PermissionError)):
        store.apply(
            f"replace-claim-{field}", actor="alice", installation="one",
            expected_versions={"task:CG-1": 0},
            changes={"claims": {"task:CG-1": {**admitted, field: value}}},
        )
    assert store.read()[1]["claims"]["task:CG-1"]["operation_id"] == claim["operation_id"]


def test_unrecognized_effect_outcome_does_not_release_claim(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind="task", scope="CG-1", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    with pytest.raises(GitCoordinationError, match="unrecognized effect outcome"):
        store.apply(
            "invalid-terminal", actor="alice", installation="one", expected_versions={},
            changes={"effects": {"run:invalid": {
                "claim": claim["operation_id"], "provider": "scheduler",
                "scope": "task:CG-1", "effect_key": "run:invalid", "outcome": "banana",
            }}},
        )


@pytest.mark.parametrize(("kind", "scope"), [("task", "CG-1"), ("phase", "demo/p1")])
def test_terminal_obligation_allows_claim_release_and_handoff(clones, kind, scope):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    key = f"{kind}:{scope}"
    if kind == "phase":
        store.transact(
            "seed-terminal-phase",
            {"actor": "alice", "installation": "one"},
            lambda state: (
                state["entities"].update({key: {
                    "kind": kind, "scope": scope, "owner": "alice",
                    "authority_generation": 1, "version": 0,
                }}),
                {},
            )[-1],
        )
    claim = GitMultiplayerClient(store, "alice", "one").claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    effect_key = f"run:terminal:{kind}"
    store.apply(
        f"terminal:{kind}", actor="alice", installation="one", expected_versions={},
        changes={
            "permits": {effect_key: {"claim": claim["operation_id"]}},
            "effects": {effect_key: {
                "claim": claim["operation_id"], "provider": "scheduler", "scope": key,
                "effect_key": effect_key, "outcome": "succeeded",
            }},
        },
    )
    store.begin_handoff(
        f"begin-terminal:{kind}", actor="alice", installation="one",
        entity_key=key, expected_version=0, pending_owner="bob",
    )
    store.acknowledge_stop(
        f"ack-terminal:{kind}", actor="alice", installation="one", entity_key=key,
    )
    store.complete_handoff(
        f"handoff-terminal:{kind}", actor="bob", installation="bob", entity_key=key,
    )
    state = store.read()[1]
    assert key not in state["claims"]
    assert state["entities"][key]["owner"] == "bob"


def test_existing_version_one_ref_without_handoff_table_upgrades_additively(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    head, state = store.read()
    state.pop("handoffs")
    state["sequence"] += 1
    legacy = store._commit(state, head, "legacy-before-handoffs")
    store._push(legacy, head)

    observed, upgraded = store.read()

    assert observed == legacy
    assert upgraded["handoffs"] == {}


def test_member_disablement_drains_task_and_phase_before_atomic_disable(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    store.apply(
        "claim-task-before-disable", actor="alice", installation="one",
        expected_versions={"task:CG-1": 0},
        changes={"claims": {"task:CG-1": {
            "operation_id": "task-claim", "kind": "task", "scope": "CG-1",
            "owner_id": "alice", "authority_generation": 1,
        }}},
    )
    store.apply(
        "claim-phase-before-disable", actor="alice", installation="one",
        expected_versions={"phase:demo/p1": 0},
        changes={"claims": {"phase:demo/p1": {
            "operation_id": "phase-claim", "kind": "phase", "scope": "demo/p1",
            "owner_id": "alice", "authority_generation": 1,
        }}},
    )

    started = store.begin_member_authority_change(
        "disable-alice", actor="admin", installation="admin",
        member_id="alice", active=False,
    )
    assert started.result == {
        "status": "draining", "entities": ["phase:demo/p1", "task:CG-1"]
    }
    _, draining = store.read()
    assert draining["members"]["alice"]["active"] is True
    assert all(draining["entities"][key]["draining"] for key in started.result["entities"])
    with pytest.raises(PermissionError, match="authority is draining"):
        store.apply(
            "new-work-while-disabling", actor="alice", installation="one",
            expected_versions={}, changes={},
        )
    with pytest.raises(GitCoordinationError, match="acknowledgement or external fence"):
        store.complete_authority_change(
            "premature-disable", actor="admin", installation="admin",
            change_key="member:alice",
        )

    for entity_key in started.result["entities"]:
        store.acknowledge_stop(
            f"ack-disable:{entity_key}", actor="alice", installation="one",
            entity_key=entity_key,
        )
    store.complete_authority_change(
        "complete-disable", actor="admin", installation="admin",
        change_key="member:alice",
    )
    _, completed = store.read()
    assert completed["members"]["alice"]["active"] is False
    assert all(completed["entities"][key]["owner"] == "" for key in started.result["entities"])
    assert all(completed["entities"][key]["authority_generation"] == 2
               for key in started.result["entities"])


def test_installation_revocation_blocks_immediately_and_requires_external_fence(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    store.apply(
        "claim-before-revoke", actor="alice", installation="two",
        expected_versions={"task:CG-1": 0},
        changes={"claims": {"task:CG-1": {
            "operation_id": "revoked-installation-claim", "kind": "task", "scope": "CG-1",
            "owner_id": "alice", "authority_generation": 1,
        }}},
    )
    store.begin_installation_revocation(
        "revoke-two", actor="admin", installation="admin", installation_id="two",
    )
    with pytest.raises(PermissionError, match="authority is draining"):
        store.apply(
            "revoked-installation-new-work", actor="alice", installation="two",
            expected_versions={}, changes={},
        )
    with pytest.raises(GitCoordinationError, match="acknowledgement or external fence"):
        store.complete_authority_change(
            "premature-revoke", actor="admin", installation="admin",
            change_key="installation:two",
        )

    store.record_external_fence(
        "fence-two", actor="admin", installation="admin", entity_key="task:CG-1",
        proof={"execution": "worker grant revoked", "publication": "push key revoked"},
    )
    store.complete_authority_change(
        "complete-revoke", actor="admin", installation="admin",
        change_key="installation:two",
    )
    _, completed = store.read()
    assert "two" not in completed["installations"]
    assert completed["revoked_installations"]["two"] == "alice"
    assert completed["entities"]["task:CG-1"]["owner"] == ""
    store.retain_late_evidence(
        "late-revoked-result", actor="alice", installation="two",
        entity_key="task:CG-1", prior_generation=1, evidence_id="late-worker-result",
        payload={"commit": "deadbeef"},
    )
    store.enroll_installation(
        "reenroll-two", actor="admin", installation="admin",
        installation_id="two", member_id="bob",
    )
    _, reenrolled = store.read()
    assert reenrolled["installations"]["two"] == "bob"
    assert "two" not in reenrolled["revoked_installations"]
    assert any(row.get("evidence_id") == "late-worker-result"
               for row in reenrolled["recovery"])


def test_live_installation_cannot_be_relabelled_to_another_member(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    with pytest.raises(GitCoordinationError, match="ownership is immutable"):
        store.enroll_installation(
            "relabel-one", actor="admin", installation="admin",
            installation_id="one", member_id="bob",
        )
    _, state = store.read()
    assert state["installations"]["one"] == "alice"


def test_project_scope_narrowing_drains_only_authority_outside_retained_scope(clones):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    started = store.begin_member_authority_change(
        "narrow-alice", actor="admin", installation="admin",
        member_id="alice", projects=["other"],
    )
    assert started.result["entities"] == ["phase:demo/p1", "task:CG-1"]
    store.complete_authority_change(
        "complete-narrow", actor="admin", installation="admin",
        change_key="member:alice",
    )
    _, completed = store.read()
    assert completed["members"]["alice"]["active"] is True
    assert completed["members"]["alice"]["project_visibility"] == "assigned"
    assert completed["members"]["alice"]["projects"] == ["other"]


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
def test_acknowledged_handoff_blocks_new_work_then_advances_generation(clones, entity_key):
    _, one, two = clones
    first = GitStateStore(one, garden_id="garden")
    kind, scope = entity_key.split(":", 1)
    client = GitMultiplayerClient(first, "alice", "one")
    client.claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )

    draining = first.begin_handoff(
        f"drain:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key, expected_version=0, pending_owner="bob",
    )
    assert draining.result == {
        "status": "blocked", "effective_owner": "alice", "pending_owner": "bob",
        "blockers": [],
    }
    state = GitStateStore(two, garden_id="garden").read()[1]
    assert state["entities"][entity_key]["owner"] == "alice"
    assert state["entities"][entity_key]["pending_owner"] == "bob"
    with pytest.raises(GitCoordinationError, match="draining"):
        first.apply(
            f"late-permit:{entity_key}", actor="alice", installation="one",
            expected_versions={}, changes={"permits": {
                f"late:{entity_key}": {
                    "claim": state["claims"][entity_key]["operation_id"],
                }
            }},
        )

    # A restarted prior installation can acknowledge its own confirmed stop.
    restarted = GitStateStore(one, garden_id="garden")
    restarted.acknowledge_stop(
        f"stop:{entity_key}", actor="alice", installation="one", entity_key=entity_key,
    )
    completed = GitStateStore(two, garden_id="garden").complete_handoff(
        f"complete:{entity_key}", actor="bob", installation="bob", entity_key=entity_key,
    )
    assert completed.result["authority_generation"] == 2
    final = first.read()[1]
    assert final["entities"][entity_key]["owner"] == "bob"
    assert not final["entities"][entity_key]["draining"]
    assert entity_key not in final["claims"]


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
@pytest.mark.parametrize(
    ("target", "member"),
    [
        ("typo", None),
        ("disabled", {"active": False, "role": "member", "project_visibility": "all"}),
        ("viewer", {"active": True, "role": "viewer", "project_visibility": "all"}),
        (
            "elsewhere",
            {
                "active": True,
                "role": "member",
                "project_visibility": "assigned",
                "projects": ["other"],
            },
        ),
    ],
)
def test_handoff_rejects_ineligible_destination_without_changing_authority(
    clones, entity_key, target, member
):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    if member is not None:
        store.transact(
            f"enroll:{target}", {},
            lambda state: (state["members"].__setitem__(target, member), {})[-1],
        )
    before = store.read()[1]

    with pytest.raises(PermissionError, match="handoff target"):
        store.begin_handoff(
            f"reject:{entity_key}:{target}", actor="alice", installation="one",
            entity_key=entity_key, expected_version=0, pending_owner=target,
        )

    after = store.read()[1]
    assert after["entities"][entity_key] == before["entities"][entity_key]
    assert entity_key not in after["handoffs"]
    assert after["recovery"] == before["recovery"]


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
def test_handoff_revalidates_destination_at_completion(clones, entity_key):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    kind, scope = entity_key.split(":", 1)
    GitMultiplayerClient(store, "alice", "one").claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.begin_handoff(
        f"drain-before-revocation:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key, expected_version=0, pending_owner="bob",
    )
    store.acknowledge_stop(
        f"stop-before-revocation:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key,
    )
    store.transact(
        f"disable-bob:{entity_key}", {},
        lambda state: (state["members"]["bob"].__setitem__("active", False), {})[-1],
    )
    before = store.read()[1]

    with pytest.raises(PermissionError, match="handoff target"):
        store.complete_handoff(
            f"reject-completion:{entity_key}", actor="admin", installation="admin",
            entity_key=entity_key,
        )

    after = store.read()[1]
    assert after["entities"][entity_key] == before["entities"][entity_key]
    assert after["handoffs"][entity_key] == before["handoffs"][entity_key]
    assert after["recovery"] == before["recovery"]


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
def test_handoff_supports_explicit_unassignment(clones, entity_key):
    _, one, _ = clones
    store = GitStateStore(one, garden_id="garden")
    kind, scope = entity_key.split(":", 1)
    GitMultiplayerClient(store, "alice", "one").claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    store.begin_handoff(
        f"drain-to-unassigned:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key, expected_version=0, pending_owner="",
    )
    store.acknowledge_stop(
        f"stop-to-unassigned:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key,
    )
    completed = store.complete_handoff(
        f"complete-unassigned:{entity_key}", actor="alice", installation="one",
        entity_key=entity_key,
    )

    assert completed.result == {"status": "complete", "owner": "", "authority_generation": 2}
    assert store.read()[1]["entities"][entity_key]["owner"] == ""


@pytest.mark.parametrize("entity_key", ["task:CG-1", "phase:demo/p1"])
def test_external_fence_requires_provider_reconciliation_and_preserves_late_evidence(
    clones, entity_key
):
    _, one, two = clones
    first = GitStateStore(one, garden_id="garden")
    kind, scope = entity_key.split(":", 1)
    claim = GitMultiplayerClient(first, "alice", "one").claim(
        kind=kind, scope=scope, owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    effect_id = "effect:provider-original"
    first.apply(
        "accepted-before-partition", actor="alice", installation="one",
        expected_versions={entity_key: 0}, changes={
            "permits": {effect_id: {"claim": claim["operation_id"]}},
            "effects": {effect_id: {
                "claim": claim["operation_id"], "provider": "source-host",
                "scope": entity_key, "effect_key": "publish", "outcome": "unknown",
            }},
        },
    )
    first.begin_handoff(
        "drain-partitioned", actor="admin", installation="admin",
        entity_key=entity_key, expected_version=0, pending_owner="bob",
    )
    recovery = GitStateStore(two, garden_id="garden")
    with pytest.raises(GitCoordinationError, match="unresolved effects"):
        recovery.record_external_fence(
            "fence-too-soon", actor="admin", installation="admin", entity_key=entity_key,
            proof={"execution": "process tree stopped", "publication": "credential revoked"},
        )
    recovery.reconcile_effect(
        "observe-original-effect", actor="bob", installation="bob",
        effect_operation_id=effect_id, outcome="fenced",
        evidence={"provider_operation_id": "provider-7", "observed": "not published"},
    )
    recovery.record_external_fence(
        "fence-partitioned", actor="admin", installation="admin", entity_key=entity_key,
        proof={"execution": "process tree stopped", "publication": "credential revoked"},
    )
    recovery.complete_handoff(
        "complete-partitioned", actor="bob", installation="bob", entity_key=entity_key,
    )
    evidence = first.retain_late_evidence(
        "late-result", actor="alice", installation="one", entity_key=entity_key,
        prior_generation=1, evidence_id="run-old-result", payload={"commit": "abc123"},
    )
    assert evidence.result == {"retained": "run-old-result", "applied": False}
    state = recovery.read()[1]
    assert state["entities"][entity_key]["owner"] == "bob"
    assert state["effects"][effect_id]["evidence"]["provider_operation_id"] == "provider-7"
    assert state["recovery"][-1]["payload"] == {"commit": "abc123"}


@pytest.mark.parametrize("enabled", ["false", 1, []])
def test_multiplayer_enabled_is_strict_boolean(tmp_path: Path, enabled):
    (tmp_path / "garden.yaml").write_text(f"multiplayer:\n  enabled: {enabled!r}\n")
    with pytest.raises(ValueError, match="must be true or false"):
        Config.load(tmp_path)


def test_two_dirty_installations_connect_without_service_and_preserve_conflicts(clones):
    remote, one, two = clones
    for repo in (one, two):
        (repo / "garden.yaml").write_text("name: shared\n")
    (one / "unrelated.txt").write_text("alex edits\n")
    (two / "tasks").mkdir()
    (two / "tasks" / "CG-1.md").write_text("blair proposal\n")

    previous = Path.cwd()
    os.chdir(one)
    try:
        result = CliRunner().invoke(cli_app, [
            "members", "connect", "garden", "alice", "one",
        ])
    finally:
        os.chdir(previous)
    assert result.exit_code == 0, result.output
    os.chdir(one)
    try:
        status = CliRunner().invoke(cli_app, ["members", "status"])
    finally:
        os.chdir(previous)
    assert status.exit_code == 0, status.output
    assert "multiplayer.enabled from garden.local.yaml" in status.output
    assert "Git: synchronized at" in status.output
    config = Config.load(one)
    client = GitMultiplayerClient.from_config(config)
    assert client is not None
    view = client.prepare(mutation=True)
    assert view.snapshot["observed_revision"]
    authority = {row["scope"]: row for row in view.snapshot["authority"]}
    assert authority["CG-2"]["owner"] == "-"
    assert authority["demo/p1"]["owner"] == "alice"
    assert view.snapshot["assignment"]["phase"] == "p1"
    assert (one / "unrelated.txt").read_text() == "alex edits\n"

    seed = GitStateStore(one, garden_id="garden")
    seed.transact(
        "publish-projection",
        {"actor": "alice", "installation": "one"},
        lambda state: (
            state.update({"projections": [{
                "kind": "task", "scope": "CG-1", "path": "tasks/CG-1.md",
                "markdown": "accepted\n", "base_revision": "missing-base", "version": 1,
            }]}),
            {},
        )[-1],
    )
    second = GitMultiplayerClient(GitStateStore(two, garden_id="garden"), "alice", "two")
    with pytest.raises(ProjectionConflict, match="tasks/CG-1.md"):
        second.prepare(mutation=True)
    assert (two / "tasks" / "CG-1.md").read_text() == "blair proposal\n"
    assert "unrelated.txt" in git(one, "status", "--short")
    head = git(remote, "rev-parse", "refs/heads/garden-state")
    git(remote, "update-ref", "-d", "refs/heads/garden-state", head)
    stale = client.refresh()
    assert stale.stale and stale.snapshot["observed_revision"]
    with pytest.raises(MultiplayerUnavailable, match="unavailable|missing"):
        client.prepare(mutation=True)


def test_enabled_mode_rejects_obsolete_coordinator_configuration(tmp_path: Path):
    (tmp_path / "garden.yaml").write_text(
        "multiplayer:\n  enabled: true\n  coordinator_url: https://obsolete.test\n"
    )
    from garden.multiplayer_client import MultiplayerClient, MultiplayerUnavailable

    with pytest.raises(MultiplayerUnavailable, match="obsolete"):
        MultiplayerClient.from_config(Config.load(tmp_path))
