from pathlib import Path

from garden.model import Status, Task, join_frontmatter, split_frontmatter


def test_roundtrip_preserves_unknown_keys(tmp_path):
    text = """---
id: X-001
title: "Hello: world"
status: ready
depends_on: [X-000]
priority: 2
reading: [a.md]
custom: 42
created: '2026-01-01T00:00:00+00:00'
updated: '2026-01-01T00:00:00+00:00'
---

## Goal

Hi.
"""
    t = Task.parse(tmp_path / "x.md", text, product="p", phase="ph")
    assert t.id == "X-001" and t.title == "Hello: world" and t.status == Status.READY
    assert t.product == "p" and t.phase == "ph"
    assert t.extra == {"custom": 42}
    t2 = Task.parse(tmp_path / "x.md", t.render())
    assert t2.to_frontmatter() == t.to_frontmatter()
    assert t2.body.strip() == "## Goal\n\nHi."


def test_log_appends_section():
    t = Task(path=Path("x"), id="A-1", title="t", body="## Goal\n\nx\n")
    t.log("first")
    t.log("second")
    assert t.body.count("## Log") == 1
    assert t.body.strip().endswith("second")


def test_split_join():
    data, body = split_frontmatter("no frontmatter")
    assert data == {} and body == "no frontmatter"
    out = join_frontmatter({"a": 1}, "\n\nbody\n\n")
    assert out == "---\na: 1\n---\n\nbody\n"


def test_default_branch():
    t = Task(path=Path("x"), id="CG-007", title="Add an Automated Review pass!")
    assert t.default_branch() == "garden/cg-007-add-an-automated-review-pass"
