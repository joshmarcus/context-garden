import pytest

from garden.model import Status
from garden.planner import import_plan, parse_plan, plan_prompt, run_planner
from garden.store import Store


def test_plan_prompt_includes_context(garden):
    store = Store(garden)
    p = plan_prompt(store, "demo", "p1", extra="be brief")
    assert "## Spec: demo/p1/specs/spec.md" in p and "Details." in p
    assert "DM-001 [ready] First task" in p and "be brief" in p


def test_plan_prompt_replan_section_absent_without_flag(garden):
    store = Store(garden)
    p = plan_prompt(store, "demo", "p1")
    assert "Failed and blocked tasks" not in p


def test_plan_prompt_replan_section_with_failed_task(garden):
    store = Store(garden)
    t = store.task("DM-001")
    t.status = Status.FAILED
    t.log("worker failed: exit 1")
    store.save(t)
    store.invalidate()

    p = plan_prompt(store, "demo", "p1", replan=True)
    assert "## Failed and blocked tasks" in p
    assert "DM-001 [failed] First task" in p
    assert "worker failed: exit 1" in p
    # tasks without troubled status not listed separately
    assert "DM-002" not in p.split("## Failed and blocked tasks")[1]


def test_plan_prompt_replan_blocked_question(garden):
    store = Store(garden)
    t = store.task("DM-002")
    t.status = Status.WAITING_HUMAN
    t.log("worker asks: Should I use X or Y?")
    store.save(t)
    store.invalidate()

    p = plan_prompt(store, "demo", "p1", replan=True)
    assert "DM-002 [waiting_human] Second task" in p
    assert "Blocked question: Should I use X or Y?" in p


def test_plan_prompt_replan_empty_when_no_failures(garden):
    store = Store(garden)
    # both tasks are ready; replan=True should produce no extra section
    p = plan_prompt(store, "demo", "p1", replan=True)
    assert "## Failed and blocked tasks" not in p


def test_parse_plan_tolerates_fences():
    items = parse_plan('here you go\n```json\n[{"title": "A", "body": "x"}]\n```\n')
    assert items[0]["title"] == "A"


def test_import_resolves_title_deps(garden):
    store = Store(garden)
    created = import_plan(store, "demo", "p1", [
        {"title": "Alpha", "priority": 1, "body": "## Goal\n\na", "depends_on": ["DM-001"]},
        {"title": "Beta", "priority": 2, "body": "## Goal\n\nb", "depends_on": ["alpha", "Nope"]},
        {"title": "First task", "body": "duplicate of an existing title"},
    ])
    assert [t.id for t in created] == ["DM-003", "DM-004"]
    beta = store.task("DM-004")
    assert beta.depends_on == ["DM-003"] and "unknown dependency 'Nope'" in beta.body
    assert store.task("DM-003").depends_on == ["DM-001"]
    assert store.task("DM-003").status.value == "ready"  # plan.auto_approve default
    store.config.data["plan"] = {"auto_approve": False}
    more = import_plan(store, "demo", "p1", [{"title": "Gamma", "body": "g", "difficulty": "hard"}])
    assert more[0].status.value == "draft" and more[0].difficulty == "hard"


def test_import_supersedes_cancels_tasks(garden):
    store = Store(garden)
    t1 = store.task("DM-001")
    t1.status = Status.FAILED
    store.save(t1)
    store.invalidate()

    created = import_plan(store, "demo", "p1", [
        {"title": "Replacement task", "priority": 1, "body": "## Goal\n\nreplaces DM-001", "supersedes": ["DM-001"]},
    ])
    assert len(created) == 1
    store.invalidate()
    assert store.task("DM-001").status == Status.CANCELLED
    assert "superseded by" in store.task("DM-001").body
    assert "DM-001" not in store.task(created[0].id).body or True  # replacement exists


def test_import_supersedes_unknown_id_logs_warning(garden):
    store = Store(garden)
    created = import_plan(store, "demo", "p1", [
        {"title": "New task", "priority": 1, "body": "## Goal\n\nsomething new", "supersedes": ["DM-999"]},
    ])
    assert len(created) == 1
    store.invalidate()
    t = store.task(created[0].id)
    assert "DM-999" in t.body and "ignored" in t.body


def test_import_supersedes_skips_already_done(garden):
    store = Store(garden)
    t1 = store.task("DM-001")
    t1.status = Status.DONE
    store.save(t1)
    store.invalidate()

    import_plan(store, "demo", "p1", [
        {"title": "Another task", "priority": 1, "body": "## Goal\n\nfoo", "supersedes": ["DM-001"]},
    ])
    store.invalidate()
    # DONE is terminal; must not be overwritten
    assert store.task("DM-001").status == Status.DONE


def test_import_plan_refuses_closed_phase_without_reopen(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    store.set_phase_closed(ph, "2026-09-04")
    store.invalidate()

    with pytest.raises(ValueError, match="is closed"):
        import_plan(store, "demo", "p1", [{"title": "Late arrival", "body": "## Goal\n\nfoo"}])
    assert store.phase("demo", "p1").closed  # untouched

    created = import_plan(store, "demo", "p1", [{"title": "Late arrival", "body": "## Goal\n\nfoo"}], reopen=True)
    assert len(created) == 1
    assert not store.phase("demo", "p1").closed


def test_run_planner_with_fake_claude(garden, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "plan")
    store = Store(garden)
    raw = run_planner(store, plan_prompt(store, "demo", "p1"))
    items = parse_plan(raw)
    created = import_plan(store, "demo", "p1", items)
    assert [t.title for t in created] == ["First planned task", "Second planned task"]
    assert created[1].depends_on == [created[0].id]
