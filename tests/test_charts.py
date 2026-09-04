from garden.charts import burnup_svg


def _ev(kind, task, at, **kw):
    return {"kind": kind, "task": task, "at": at, **kw}


def test_burnup_counts_tasks_done_now_once():
    events = [
        _ev("dispatch", "T-1", "2026-09-01T09:00:00+00:00"),
        _ev("transition", "T-1", "2026-09-01T11:00:00+00:00", to="done"),
        _ev("transition", "T-1", "2026-09-02T09:00:00+00:00", to="running"),  # reopened
        _ev("transition", "T-1", "2026-09-03T09:00:00+00:00", to="done"),
        _ev("transition", "T-2", "2026-09-02T12:00:00+00:00", to="done"),
        _ev("pr_opened", "T-3", "2026-09-02T13:00:00+00:00"),
    ]
    raw = burnup_svg(events, 3)
    assert "merged (3)" in raw  # every transition, the historical view
    now = burnup_svg(events, 3, done_ids={"T-1"})
    assert "merged (1)" in now  # T-2 is not done any more, T-1 counts once
    assert "PRs opened (1)" in now
    assert "No merges yet" in burnup_svg([], 3, done_ids=set())
