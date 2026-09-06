"""Render the supplied Now 2 snapshot and separate simulated state atlas offline."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from garden.charts import sparkline_svg
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


def matrices(period: dict) -> list[dict]:
    """Preserve exported measurements; compute only formatting and relative shades."""
    result = []
    denominators = {
        "cost_per_accepted": "accepted tasks",
        "work_run_cost": "work runs",
        "first_pass": "reviewed tasks (export cohort)",
        "revise_rounds": "accepted tasks",
        "lead_time": "accepted tasks with lead times",
    }
    for metric in period["tiers"]["metrics"]:
        unit = {"usd": "USD", "pct": "%", "hours": "min", "rounds": "rounds"}[metric["unit"]]
        multiplier = {"pct": 100, "hours": 60}.get(metric["unit"], 1)
        rendered = []
        for difficulty in ("easy", "medium", "hard"):
            entries = metric["rows"].get(difficulty, {})
            values = [entry["value"] for entry in entries.values() if entry["value"] is not None]
            lo, hi = (min(values), max(values)) if values else (0, 0)
            cells = []
            for model in period["tiers"]["models"]:
                entry = entries.get(model, {"value": None, "n": 0})
                value, n = entry["value"], entry["n"]
                rank, quality = "missing", .5
                if value is not None:
                    rank = "equal"
                    if hi != lo:
                        quality = (hi - value) / (hi - lo)
                        if metric["better"] == "high":
                            quality = 1 - quality
                        rank = "best" if quality == 1 else "worst" if quality == 0 else "between"
                cells.append(cell(value * multiplier if value is not None else None, n, unit, rank, quality * 100))
            rendered.append({"difficulty": difficulty, "cells": cells})
        result.append({"key": metric["key"], "title": metric["label"].capitalize(), "unit": unit,
                       "direction": "higher" if metric["better"] == "high" else "lower",
                       "denominator": denominators[metric["key"]], "rows": rendered})
    return result


def elapsed(start: str, end: str) -> str:
    seconds = max(0, int((dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02}"
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def context() -> dict:
    snapshot = json.loads((HERE / "snapshot.json").read_text())
    period = snapshot["period"]["hour"]
    ctx = {
        "defs": Markup(DEFS), "server_now": snapshot["captured_at"], "matrices": matrices(period),
        "snapshot": snapshot, "period": period, "phase": snapshot["where"]["primary"],
        "periods": [{"key": key, "label": label, "data": snapshot["period"][key],
                     "matrices": matrices(snapshot["period"][key]),
                     "models": snapshot["period"][key]["tiers"]["models"],
                     "spark": Markup(sparkline_svg(snapshot["period"][key]["throughput"], width=300, height=40))}
                    for key, label in [("hour", "Last hour"), ("today", "Today (UTC)"),
                                       ("24h", "Last 24 hours"), ("phase", "This phase · phase-05")]],
        "models": period["tiers"]["models"],
        "examples": [
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

    ctx["runs"] = []
    for run in snapshot["now"]:
        cost = "— · not reported yet" if run["spend_usd"] is None else f"${run['spend_usd']:.2f} · at capture"
        ctx["runs"].append({
            "run_id": f"{run['task']}:{run['run']}", "title": run["title"],
            "mode": run["mode"], "state": run["state"], "harness": run["harness"] or "harness not recorded",
            "model": run["model"] or "model not recorded", "tier": run["difficulty"] or "tier not recorded",
            "started_at": run["started_at"], "finished_at": "", "typical": run["typical_s"] or 0,
            "elapsed": elapsed(run["started_at"], snapshot["captured_at"]),
            "line": ("Other design's excerpt omitted to preserve independent design." if run["task"] == "CG-307"
                     else run["said"] or "No assistant text recorded."),
            "cost": cost, "no_process": run["no_process"],
            "task_url": f"/tasks/{run['task']}",
            "run_url": f"/runs/{run['task']}/{run['run']}",
        })
    return ctx


def render() -> str:
    return environment().get_template("page.html.j2").render(**context())


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"Rendered {OUTPUT.relative_to(HERE.parents[2])} (supplied snapshot and simulated atlas)")
