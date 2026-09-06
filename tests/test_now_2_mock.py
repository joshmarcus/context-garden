"""The offline design's run-clock contract; no live garden or browser required."""

from __future__ import annotations

import ast
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


def test_now_2_generator_delegates_matrices_to_events() -> None:
    # Inspect provenance only: this source must never read the live garden in tests.
    source = ast.parse((RENDERER.parent / "snapshot_generator_from_now1.py").read_text())
    assert any(isinstance(node, ast.ImportFrom) and node.module == "garden.events"
               and any(alias.name == "difficulty_by_model" for alias in node.names)
               for node in source.body)
    period = next(node for node in source.body
                  if isinstance(node, ast.FunctionDef) and node.name == "period")
    result = next(node.value for node in period.body if isinstance(node, ast.Return))
    tiers = next(value for key, value in zip(result.keys, result.values, strict=True)
                 if isinstance(key, ast.Constant) and key.value == "tiers")
    assert ast.unparse(tiers) == "difficulty_by_model(events, tasks, since)"


def test_now_2_preserves_every_exported_metric_cell() -> None:
    renderer = runpy.run_path(str(RENDERER))
    for period in renderer["context"]()["snapshot"]["period"].values():
        for source, rendered in zip(period["tiers"]["metrics"], renderer["matrices"](period), strict=True):
            factor = {"pct": 100, "hours": 60}.get(source["unit"], 1)
            for row in rendered["rows"]:
                entries = source["rows"].get(row["difficulty"], {})
                values = [e["value"] for e in entries.values() if e["value"] is not None]
                for model, cell in zip(period["tiers"]["models"], row["cells"], strict=True):
                    entry = entries.get(model, {"value": None, "n": 0})
                    value = entry["value"]
                    assert cell["n"] == entry["n"]
                    if value is None:
                        assert cell["display"] == "—"
                        assert cell["rank"] == "missing"
                        continue
                    expected = {"usd": lambda v: f"${v:.2f}", "pct": lambda v: f"{v:.0f}%",
                                "hours": lambda v: f"{v:.0f} min", "rounds": lambda v: f"{v:.1f}"}
                    assert cell["display"] == expected[source["unit"]](value * factor)
                    if min(values) == max(values):
                        assert cell["rank"] == "equal"
                    else:
                        best = max(values) if source["better"] == "high" else min(values)
                        worst = min(values) if source["better"] == "high" else max(values)
                        assert cell["rank"] == ("best" if value == best else "worst" if value == worst else "between")
