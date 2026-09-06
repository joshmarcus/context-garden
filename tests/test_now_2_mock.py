"""The offline design's run-clock contract; no live garden or browser required."""

from __future__ import annotations

import runpy
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "docs/design/now-2/render.py"


class RunAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "article" and "data-run-id" in data:
            self.rows.append(data)


def test_now_2_card_renders_clock_start_and_finish_attributes() -> None:
    renderer = runpy.run_path(str(RENDERER))
    env = renderer["environment"]()
    run = dict(renderer["context"]()["examples"][0])
    run.update(started_at="2026-09-06T02:41:42+00:00", finished_at="2026-09-06T02:45:01+00:00",
               title='<script>alert("title")</script>')
    html = env.get_template("card.html.j2").module.card(run)
    parser = RunAttributes()
    parser.feed(html)
    assert parser.rows[0]["data-started-at"] == "2026-09-06T02:41:42+00:00"
    assert parser.rows[0]["data-finished-at"] == "2026-09-06T02:45:01+00:00"
    assert parser.rows[0]["data-typical-seconds"] == "480"
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_now_2_committed_mock_matches_renderer() -> None:
    renderer = runpy.run_path(str(RENDERER))
    assert renderer["OUTPUT"].read_text() == renderer["render"]()


def test_now_2_snapshot_keeps_distinct_runs_and_process_uncertainty() -> None:
    renderer = runpy.run_path(str(RENDERER))
    context = renderer["context"]()
    html = renderer["render"]()
    parser = RunAttributes()
    parser.feed(html)
    rows = {row["data-run-id"]: row for row in parser.rows}
    for run in context["snapshot"]["now"]:
        key = f"{run['task']}:{run['run']}"
        assert rows[key]["data-started-at"] == run["started_at"]
    assert html.count("Process not found · run record still running") == 3
    assert "Queue reason conflicts with available capacity" in html
