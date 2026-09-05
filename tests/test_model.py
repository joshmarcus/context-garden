from pathlib import Path

import pytest

from garden.model import Status, Task, ensure_open, join_frontmatter, split_frontmatter


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


def test_ensure_open_allows_active_tasks():
    for status in (Status.DRAFT, Status.READY, Status.RUNNING, Status.IN_REVIEW, Status.CHANGES_REQUESTED, Status.WAITING_HUMAN, Status.FAILED):
        ensure_open(Task(path=Path("x"), id="A-1", title="t", status=status))  # must not raise


def test_ensure_open_refuses_a_merged_done_task():
    """CG-142: the refusal names the state and, for a merge, the PR number and time."""
    t = Task(path=Path("x"), id="CG-074", title="t", status=Status.DONE, pr="https://github.com/o/r/pull/71",
             body="## Log\n\n- 2026-09-05T02:17:55+00:00 PR merged: https://github.com/o/r/pull/71\n")
    with pytest.raises(RuntimeError, match=r"CG-074 is done: #71 was merged at 02:17:55"):
        ensure_open(t)


def test_ensure_open_refuses_a_cancelled_task_with_its_last_log_line():
    t = Task(path=Path("x"), id="CG-005", title="t", status=Status.CANCELLED,
             body="## Log\n\n- 2026-01-01T00:00:00+00:00 cancelled (web)\n")
    with pytest.raises(RuntimeError, match=r"CG-005 is cancelled: cancelled \(web\) at 00:00:00"):
        ensure_open(t)


def test_default_branch():
    t = Task(path=Path("x"), id="CG-007", title="Add an Automated Review pass!")
    assert t.default_branch() == "garden/cg-007-add-an-automated-review-pass"


def test_elapsed_minutes_never_negative(tmp_path):
    from garden.runs import Run

    r = Run(task_id="A", run_id="r", dir=str(tmp_path), runner="local",
            started_at="2026-09-04T11:44:28.900000+00:00", finished_at="2026-09-04T11:44:28+00:00")
    assert r.elapsed_minutes() == 0.0
