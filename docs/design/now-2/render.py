"""Render the offline Now 2 design simulation; never reads controller state."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from garden.plants import DEFS, stage_svg

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parents[2] / "src/garden/web/static/mock/now-2.html"
SERVER_NOW = "2026-09-06T02:45:00+00:00"


def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE), autoescape=True, undefined=StrictUndefined,
        trim_blocks=True, lstrip_blocks=True,
    )
    env.globals["stage"] = lambda state: Markup(stage_svg(state, 22))
    return env


def cell(value: float | None, n: int, unit: str, rank: str, shade: float) -> dict:
    if value is None:
        display = "—"
    elif unit == "USD":
        display = f"${value:.2f}"
    elif unit == "%":
        display = f"{value:.0f}%"
    elif unit == "min":
        display = f"{value:.0f} min"
    else:
        display = f"{value:.1f}"
    return {"display": display, "n": n, "rank": rank, "shade": shade}


def matrices() -> list[dict]:
    """Illustrative values, not garden metrics. Only derive presentation ranks here."""
    examples = [
        ("total", "Total cost / accepted task", "USD", "lower", "accepted tasks with complete prices",
         [[(2.4, 8), (4.8, 5), (3.1, 2)], [(5.2, 4), (4.1, 6), (8.7, 2)], [(None, 0), (12.8, 3), (12.8, 1)]]),
        ("work", "Mean work-run cost", "USD", "lower", "priced work runs",
         [[(.8, 12), (2.1, 9), (1.2, 2)], [(2.3, 7), (1.8, 8), (4.2, 2)], [(None, 0), (5.4, 3), (8.1, 1)]]),
        ("first", "First-pass approval", "%", "higher", "reviewed accepted tasks",
         [[(75, 8), (80, 5), (100, 2)], [(75, 4), (100, 6), (50, 2)], [(None, 0), (67, 3), (100, 1)]]),
        ("revise", "Mean revise rounds", "rounds", "lower", "accepted tasks with dispatch history",
         [[(.3, 8), (.2, 5), (0, 2)], [(.5, 4), (0, 6), (1, 2)], [(None, 0), (.3, 3), (0, 1)]]),
        ("lead", "Median lead time", "min", "lower", "accepted tasks with both timestamps",
         [[(14, 8), (22, 5), (12, 2)], [(32, 4), (26, 6), (48, 2)], [(None, 0), (67, 3), (80, 1)]]),
    ]
    result = []
    for key, title, unit, direction, denominator, rows in examples:
        rendered = []
        for difficulty, row in zip(("easy", "medium", "hard"), rows, strict=True):
            values = [value for value, _ in row if value is not None]
            lo, hi = min(values), max(values)
            cells = []
            for value, n in row:
                rank, quality = "missing", .5
                if value is not None:
                    rank = "equal"
                    if hi != lo:
                        quality = (hi - value) / (hi - lo)
                        if direction == "higher":
                            quality = 1 - quality
                        rank = "best" if quality == 1 else "worst" if quality == 0 else "between"
                cells.append(cell(value, n, unit, rank, quality * 100))
            rendered.append({"difficulty": difficulty, "cells": cells})
        result.append({"key": key, "title": title, "unit": unit, "direction": direction,
                       "denominator": denominator, "rows": rendered})
    return result


def context() -> dict:
    return {
        "defs": Markup(DEFS), "server_now": SERVER_NOW, "matrices": matrices(),
        "models": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-6-astra"],
        "runs": [
            {"run_id": "simulation-work", "title": "Draft a garden from an existing repository",
             "mode": "work", "state": "running", "harness": "codex", "model": "gpt-5.6-terra",
             "tier": "medium", "started_at": "2026-09-06T02:41:42+00:00", "finished_at": "",
             "typical": 480, "elapsed": "3:18", "line": "Validating the generated context against the project.",
             "cost": "$1.24 · recorded at 02:44 UTC"},
            {"run_id": "simulation-review", "title": "Every PR reaches the review queue",
             "mode": "review", "state": "in_review", "harness": "codex", "model": "gpt-5.6-luna",
             "tier": "easy", "started_at": "2026-09-06T02:43:12+00:00", "finished_at": "",
             "typical": 180, "elapsed": "1:48", "line": "Checking that a superseded review cannot apply its verdict.",
             "cost": "— · not reported yet"},
            {"run_id": "simulation-finish", "title": "Record the cost of an accepted task",
             "mode": "work finished · checks next", "state": "done", "harness": "codex", "model": "gpt-5.6-terra",
             "tier": "medium", "started_at": "2026-09-06T02:34:00+00:00", "finished_at": "2026-09-06T02:44:57+00:00",
             "typical": 0, "elapsed": "10:57", "line": "Implementation complete. The scheduler will run the checks.",
             "cost": "$2.36 · final"},
        ],
    }


def render() -> str:
    return environment().get_template("page.html.j2").render(**context())


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"Rendered {OUTPUT.relative_to(HERE.parents[2])} (simulation)")
