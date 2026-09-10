from __future__ import annotations

import pytest
import yaml

from garden.configuration import apply_changes, validate_configuration
from garden.model import Status
from garden.scheduler.report import TickReport


def _add_second_phase(garden, write, *, depends_on: list[str] | None = None):
    write(garden / "demo" / "p2" / "goals.md", "# p2\n\nNext phase.\n")
    write(garden / "demo" / "p2" / "tasks" / "DM-010-next.md", f"""
        ---
        id: DM-010
        title: Next phase task
        status: ready
        depends_on: {depends_on or []}
        priority: 1
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the next thing.
        """)


def _sequential(sched):
    sched.cfg.data["phase_execution"] = "sequential"
    sched.store.invalidate()


def test_phase_execution_is_validated_shared_project_configuration(garden):
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    assert "phase_execution" not in data

    changed = apply_changes(data, {"phase_execution": "sequential"}, product="demo")
    validate_configuration(changed)
    assert changed["products"]["demo"]["configuration"]["overrides"]["phase_execution"] == "sequential"

    try:
        apply_changes(data, {"phase_execution": "serial"})
    except ValueError as error:
        assert "must be one of concurrent, sequential" in str(error)
    else:  # pragma: no cover - guards the validation contract
        raise AssertionError("invalid phase execution mode was accepted")


def test_sequential_queue_waits_for_explicit_close_and_reopening(garden, sched):
    from tests.conftest import write

    _add_second_phase(garden, write)
    assert sched.sequential_phase("demo") is None  # legacy concurrent behavior is the default
    _sequential(sched)
    later = sched.store.task("DM-010")

    assert sched.sequential_phase("demo").name == "p1"
    assert "demo/p1 to close" in sched.phase_admission_refusal(later)
    assert "required closing review or explicit phase closure" not in sched.phase_admission_refusal(later)
    assert "waiting: demo/p2 waits for sequential phase demo/p1" in next(
        why for task, _mode, why in sched.dispatch_queue() if task.id == later.id
    )
    sched.state.get(later.id)["pending_reviews"] = [{"kind": "persona", "name": "designer"}]
    assert sched.review_wait_reason(later)[0] == "phase"

    first = sched.store.phase("demo", "p1")
    for task in first.tasks:
        task.status = Status.DONE
        sched.store.save(task)
    sched.store.invalidate()
    assert sched.sequential_phase("demo").name == "p1"
    assert "required closing review or explicit phase closure" in sched.phase_admission_refusal(
        sched.store.task("DM-010")
    )

    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "2026-09-10")
    sched.store.invalidate()
    assert sched.sequential_phase("demo").name == "p2"
    assert sched.phase_admission_refusal(sched.store.task("DM-010")) == ""

    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "")
    sched.store.invalidate()
    assert sched.sequential_phase("demo").name == "p1"
    assert "demo/p1 to close" in sched.phase_admission_refusal(sched.store.task("DM-010"))


def test_sequential_gate_covers_direct_model_routes_and_keeps_active_runs(garden, sched):
    from fastapi.testclient import TestClient

    from garden.store import Store
    from garden.web.app import create_app
    from tests.conftest import write

    _add_second_phase(garden, write)
    sched.store.invalidate()
    later = sched.store.task("DM-010")
    active = sched.runs.new_run(later.id, "remote", mode="work")
    active.status = "running"
    active.save()
    _sequential(sched)
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["phase_execution"] = "sequential"
    (garden / "garden.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    assert sched.runs.latest(later.id).status == "running"
    page = TestClient(create_app(Store(garden), watch=False, host="testserver")).get(
        f"/tasks/{later.id}"
    ).text
    assert "Held by sequential phase order" in page
    assert "will not start, revise, review, or rebase" in page
    assert "Work already in flight may finish and publish" in page
    assert "or merge" not in page
    # Collection/publication recovery retains the pre-existing closed/frozen-only gate.
    sched._refuse_if_closed_or_frozen(later)
    for dispatch in (
        lambda: sched.dispatch(later),
        lambda: sched.dispatch_review(later),
        lambda: sched.start_trial(later, ["claude:a", "claude:b"]),
        lambda: sched.dispatch_persona_phase(sched.store.phase("demo", "p2"), "designer"),
        lambda: sched.start_kickoff(sched.store.phase("demo", "p2")),
    ):
        try:
            dispatch()
        except RuntimeError as error:
            assert "waits for sequential phase demo/p1 to close" in str(error)
        else:  # pragma: no cover - every local/remote model route shares this gate
            raise AssertionError("later-phase model work was admitted")

    sched.cfg.data["phase_execution"] = "concurrent"
    assert sched.phase_admission_refusal(later) == ""
    assert sched.runs.latest(later.id).status == "running"


def test_pending_investigation_waits_across_enable_and_phase_reopen(garden, sched, monkeypatch):
    from tests.conftest import write

    _add_second_phase(garden, write)
    sched.store.invalidate()
    first = sched.store.phase("demo", "p1")
    later = sched.store.task("DM-010")
    investigation = {
        "owner": "agent",
        "status": "requested",
        "reason": "diagnose the later phase",
    }
    sched.state.get(later.id)["investigation"] = investigation
    sched.state.save()
    monkeypatch.setattr(sched, "slots_free", lambda: 1)
    monkeypatch.setattr(sched, "local_slots_free", lambda: 1)
    calls = []
    monkeypatch.setattr(
        sched, "dispatch_investigation", lambda task, runner=None: calls.append(task.id)
    )

    # Enabling sequential mode before the pending request launches defers it without
    # converting the retryable request into a failed investigation.
    _sequential(sched)
    sched._dispatch_pending_investigations({later.id: later}, TickReport())
    assert calls == []
    assert investigation["status"] == "requested"

    sched.store.set_phase_closed(first, "2026-09-10")
    sched.store.invalidate()
    sched._dispatch_pending_investigations({later.id: later}, TickReport())
    assert calls == [later.id]

    # Reopening the earlier phase updates admission immediately and preserves the pending
    # request until that phase closes again.
    calls.clear()
    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "")
    sched.store.invalidate()
    sched._dispatch_pending_investigations({later.id: later}, TickReport())
    assert calls == []
    assert investigation["status"] == "requested"
    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "2026-09-10")
    sched.store.invalidate()
    sched._dispatch_pending_investigations({later.id: later}, TickReport())
    assert calls == [later.id]


def test_direct_investigation_obeys_sequential_order(garden, sched):
    from tests.conftest import write

    _add_second_phase(garden, write)
    sched.store.invalidate()
    later = sched.store.task("DM-010")
    _sequential(sched)
    with pytest.raises(RuntimeError, match="waits for sequential phase demo/p1 to close"):
        sched.dispatch(later, mode="investigation")


def test_trial_comparison_waits_when_an_earlier_phase_reopens(garden, sched, fake_github):
    from fastapi.testclient import TestClient

    from garden.store import Store
    from garden.web.app import create_app
    from tests.conftest import write

    _add_second_phase(garden, write)
    sched.store.invalidate()
    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "2026-09-10")
    _sequential(sched)
    later = sched.store.task("DM-010")
    sched.start_trial(later, ["claude:sonnet", "claude:opus"])

    # Existing contender work finishes, but reopening the earlier phase prevents the new
    # comparison model run from starting and leaves a visible, retryable trial state.
    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "")
    sched.store.invalidate()
    rep = sched.tick()
    trial = sched.state.get(later.id)["trial"]
    assert trial["status"] == "comparison_deferred"
    assert "waits for sequential phase demo/p1 to close" in trial["compare_deferred"]
    assert f"{later.id}(compare)" not in rep.dispatched
    assert not any(run.mode == "compare" for run in sched.runs.runs_for(later.id))
    page = TestClient(create_app(Store(garden), watch=False, host="testserver")).get(
        f"/tasks/{later.id}"
    ).text
    assert "comparison deferred" in page
    assert "waits for sequential phase demo/p1 to close" in page

    # Closing the selected phase again retries from the preserved contender PRs.
    sched.store.set_phase_closed(sched.store.phase("demo", "p1"), "2026-09-10")
    sched.store.invalidate()
    rep = sched.tick()
    assert sched.state.get(later.id)["trial"]["status"] == "comparing"
    assert "compare_deferred" not in sched.state.get(later.id)["trial"]
    assert f"{later.id}(compare)" in rep.dispatched


def test_sequential_phase_does_not_skip_freeze_or_later_dependency(garden, sched):
    from tests.conftest import write

    _add_second_phase(garden, write)
    first = sched.store.task("DM-001")
    first.depends_on = ["DM-010"]
    sched.store.save(first)
    phase = sched.store.phase("demo", "p1")
    sched.store.set_phase_frozen(phase, "2026-09-10")
    _sequential(sched)

    current = sched.sequential_phase("demo")
    assert current is not None and current.name == "p1"
    assert "is frozen" in sched.sequential_phase_wait_reason(current)
    sched.store.set_phase_frozen(current, "")
    sched.store.invalidate()
    assert "cross-phase dependencies are still open: DM-010" in sched.sequential_phase_wait_reason(
        sched.sequential_phase("demo")
    )
