
from garden.planner import import_plan, parse_plan, plan_prompt, run_planner
from garden.store import Store


def test_plan_prompt_includes_context(garden):
    store = Store(garden)
    p = plan_prompt(store, "demo", "p1", extra="be brief")
    assert "## Spec: demo/p1/specs/spec.md" in p and "Details." in p
    assert "DM-001 [ready] First task" in p and "be brief" in p


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
    assert store.task("DM-003").status.value == "draft"


def test_run_planner_with_fake_claude(garden, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "plan")
    store = Store(garden)
    raw = run_planner(store, plan_prompt(store, "demo", "p1"))
    items = parse_plan(raw)
    created = import_plan(store, "demo", "p1", items)
    assert [t.title for t in created] == ["First planned task", "Second planned task"]
    assert created[1].depends_on == [created[0].id]
