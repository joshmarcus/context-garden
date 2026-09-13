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
                        "active": True,
                        "assignment": {
                            "member_id": "alice", "project": "demo", "phase": "p1",
                            "enabled": True, "generation": 1,
                        },
                    },
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
                    },
                    "task:CG-2": {
                        "kind": "task", "scope": "CG-2", "owner": "-",
                        "authority_generation": 1, "version": 0,
                    },
                    "phase:demo/p1": {
                        "kind": "phase", "scope": "demo/p1", "owner": "alice",
                        "authority_generation": 1, "version": 0,
                    },
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
    if atomic:
        replacement_changes["entities"] = {
            key: {"owner": "bob", "authority_generation": 2}
        }
    with pytest.raises(GitCoordinationError, match="claim operation_id is immutable"):
        store.apply(
            f"replace:{effect_key}", actor="alice", installation="one",
            expected_versions={key: 0}, changes=replacement_changes,
        )

    with pytest.raises(GitCoordinationError, match="unresolved permits or effects"):
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
    store.apply(
        f"handoff-terminal:{kind}", actor="alice", installation="one",
        expected_versions={key: 0},
        changes={
            "claims": {key: None},
            "entities": {key: {"owner": "bob", "authority_generation": 2}},
        },
    )
    state = store.read()[1]
    assert key not in state["claims"]
    assert state["entities"][key]["owner"] == "bob"


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
