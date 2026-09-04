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
    assert out.startswith("<svg") and 'href="/tasks/B-2"' in out and out.count("<rect") == 3 and out.count("marker-end") == 2
