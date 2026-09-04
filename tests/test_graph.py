from pathlib import Path

import pytest

from garden.graph import (
    GraphError,
    blockers,
    critical_path,
    effective_status,
    mermaid,
    ready,
    topological_order,
    validate,
)
from garden.model import Status, Task


def T(id, deps=(), status="ready", pri=3):
    return Task(path=Path(id), id=id, title=f"task {id}", status=Status(status), depends_on=list(deps), priority=pri)


def test_ready_and_blocked():
    tasks = {t.id: t for t in [T("A", status="done"), T("B", ["A"], pri=2), T("C", ["B"]), T("D", pri=1), T("E", status="draft")]}
    assert [t.id for t in ready(tasks)] == ["D", "B"]
    assert blockers(tasks["C"], tasks) == ["B"]
    assert effective_status(tasks["C"], tasks) == "blocked"
    assert effective_status(tasks["E"], tasks) == "draft"


def test_cycle_and_unknown():
    tasks = {t.id: t for t in [T("A", ["B"]), T("B", ["A"]), T("C", ["Z"])]}
    probs = validate(tasks)
    assert any("unknown task Z" in p for p in probs)
    assert any("cycle" in p for p in probs)
    with pytest.raises(GraphError):
        topological_order(tasks)


def test_topo_and_critical_path():
    tasks = {t.id: t for t in [T("A", status="done"), T("B", ["A"]), T("C", ["B"]), T("D")]}
    assert topological_order(tasks) == ["A", "D", "B", "C"]
    assert critical_path(tasks) == ["B", "C"]


def test_mermaid():
    tasks = {t.id: t for t in [T("A-1"), T("B-2", ["A-1"])]}
    m = mermaid(tasks)
    assert "A_1 --> B_2" in m and "graph LR" in m


def test_svg():
    tasks = {t.id: t for t in [T("A-1", status="done"), T("B-2", ["A-1"]), T("C-3", ["A-1"], status="draft")]}
    from garden.graph import svg

    out = svg(tasks)
    assert out.startswith("<svg") and 'href="/tasks/B-2"' in out and out.count("<circle class=\"halo\"") == 3
    assert out.count("<path d=") == 3  # A-1 rises from the ground; two dependency vines
    assert 'href="#st-fruit"' in out and 'href="#st-sprout"' in out and 'href="#st-seed"' in out


def test_visible_ids_hides_done_and_cancelled():
    from garden.graph import visible_ids

    tasks = {t.id: t for t in [T("A", status="done"), T("B", ["A"]), T("C", status="cancelled")]}
    assert visible_ids(tasks) == {"A", "B", "C"}
    assert visible_ids(tasks, hide_done=True) == {"B"}


def test_layers_hide_done_closes_gaps():
    from garden.graph import layers, visible_ids

    tasks = {t.id: t for t in [T("A-1", status="done"), T("B-2", ["A-1"]), T("C-3", ["B-2"])]}
    vis = visible_ids(tasks, hide_done=True)
    assert layers(tasks, vis) == {"B-2": 0, "C-3": 1}


def test_svg_hide_done_relayouts_and_notes_hidden_dep():
    from garden.graph import svg

    tasks = {t.id: t for t in [T("A-1", status="done"), T("B-2", ["A-1"])]}
    out = svg(tasks, hide_done=True)
    assert out.count('<circle class="halo"') == 1
    assert 'href="/tasks/B-2"' in out
    assert "depends on hidden: A-1" in out
    # B-2's only dependency is hidden, so it draws as a root (no vine reaching another node)
    assert out.count("<path d=") == 1


def test_svg_hide_done_all_hidden_returns_minimal():
    from garden.graph import svg

    tasks = {t.id: t for t in [T("A", status="done")]}
    out = svg(tasks, hide_done=True)
    assert out.startswith("<svg") and "halo" not in out


def test_mermaid_visible_filters_nodes_and_edges():
    from garden.graph import visible_ids

    tasks = {t.id: t for t in [T("A-1", status="done"), T("B-2", ["A-1"])]}
    vis = visible_ids(tasks, hide_done=True)
    m = mermaid(tasks, visible=vis)
    assert "A_1" not in m
    assert "B_2" in m
    assert "-->" not in m


def test_every_status_renders_in_svg_and_mermaid():
    from garden.graph import svg

    tasks = {}
    prev = None
    for i, st in enumerate(Status):
        tid = f"S-{i}"
        tasks[tid] = T(tid, [prev] if prev else (), status=st.value)
        prev = tid
    out = svg(tasks)
    assert out.count("<circle class=\"halo\"") == len(list(Status))
    m = mermaid(tasks)
    assert m.count("style ") == len(list(Status))
