"""Serve a disposable README example; never starts workers or contacts GitHub.

Run with PYTHONPATH=src python scripts/readme_demo.py, then capture localhost:8876.
All tasks, run records, and costs are fictional. The temporary garden is removed on exit.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import uvicorn
import yaml

from garden.events import EventLog
from garden.model import Status, Task
from garden.qa.sandbox import MemoryGitHub, make_garden
from garden.runs import RunStore
from garden.scheduler import State
from garden.store import Store
from garden.web.app import create_app


def example(root: Path) -> Store:
    garden = make_garden(root)
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update(name="Fieldnotes · example garden", runner="local", max_parallel=2)
    config["products"]["demo"]["runner"] = "local"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    (garden / "demo/product.md").write_text(
        "# Fieldnotes\n\nA small app for collecting field observations, with search and CSV export.\n"
        "\n## Conventions\n\nKeep existing notebooks readable. Test imports with malformed rows.\n"
    )
    phase = garden / "demo/p1"
    (phase / "goals.md").write_text(
        "---\nplant: pea\nlatin: Pisum sativum\nplate: I\n---\n\n"
        "# Search and export\n\nFind observations quickly and take a copy of your notebook anywhere.\n\n"
        "## Goals\n\n- Search observations by text and date.\n- Export a notebook as CSV.\n"
        "- Preserve existing notebooks and report invalid imports clearly.\n\n"
        "## Definition of done\n\nSearch and export work together, with tests and user documentation.\n"
    )
    (phase / "specs/spec.md").write_text(
        "# Search and export\n\nSearch matches observation text and supports date ranges. "
        "CSV exports include the observation date, notebook, text, and tags.\n"
    )
    store = Store(garden)
    rows = [
        ("DM-001", "Add notebook storage", Status.DONE, [], "easy"),
        ("DM-002", "Choose the CSV export scope", Status.WAITING_HUMAN, ["DM-001"], "medium"),
        ("DM-003", "Search observations by text", Status.READY, ["DM-001"], "medium"),
        ("DM-004", "Filter observations by date", Status.READY, ["DM-003"], "easy"),
        ("DM-005", "Export a notebook as CSV", Status.READY, ["DM-002"], "medium"),
        ("DM-006", "Explain invalid import rows", Status.DRAFT, ["DM-001"], "easy"),
        ("DM-007", "Document search and export", Status.DRAFT, ["DM-004", "DM-005"], "easy"),
    ]
    existing = store.tasks()
    for tid, title, status, deps, difficulty in rows:
        task = existing.get(tid) or Task(path=phase / "tasks" / f"{tid}.md", id=tid, title=title)
        task.title, task.status, task.depends_on = title, status, deps
        task.product, task.phase, task.difficulty = "demo", "p1", difficulty
        task.runner = "local"
        task.reading = ["demo/p1/specs/spec.md"]
        task.created = task.updated = "2026-09-09T09:00:00+00:00"
        task.body = (
            f"## Goal\n\n{title}.\n\n## Context\n\nPart of the search and export release for Fieldnotes.\n\n"
            "## Acceptance criteria\n\n- [ ] Existing notebooks remain readable.\n"
            "- [ ] The documented behavior has automated test coverage.\n\n"
            "## Out of scope\n\nCloud sync and shared notebooks.\n"
        )
        if tid == "DM-002":
            task.body = (
                "## Goal\n\nDefine which observations a CSV export includes.\n\n"
                "## Context\n\nSearch can narrow the notebook before the user chooses Export. "
                "The export needs a clear rule for active filters.\n\n"
                "## Acceptance criteria\n\n- [ ] The export scope is explicit in the button label.\n"
                "- [ ] A test covers export while a search filter is active.\n\n"
                "## Out of scope\n\nNew file formats and scheduled exports.\n"
            )
        store.save(task)
    state = State(garden / ".garden/state.json")
    state.get("DM-002")["question"] = (
        "Should Export include every observation in the notebook, or only the current search results?"
    )
    state.save()
    events = EventLog(garden / ".garden/events.jsonl")
    events.emit("dispatch", "DM-001", at="2026-09-09T09:00:00+00:00")
    events.emit("pr_opened", "DM-001", at="2026-09-09T10:00:00+00:00")
    events.emit("transition", "DM-001", at="2026-09-09T11:00:00+00:00",
                to="done", base_merged=True, note="PR merged into main")
    runs = RunStore(garden / ".garden")
    for index, (tid, mode, amount) in enumerate([
        ("DM-001", "work", 0.42), ("DM-001", "review", 0.16),
        ("DM-002", "work", 0.31), ("DM-001", "revise", 0.12),
    ]):
        run = runs.new_run(tid, "manual", mode, run_id=f"20260909T10{index:02}00Z-{mode}")
        run.status = "done"
        run.harness, run.model, run.difficulty = "claude", "sample-model", "medium"
        run.started_at = f"2026-09-09T10:{index:02}:00+00:00"
        run.finished_at = f"2026-09-09T10:{index:02}:45+00:00"
        run.cost_usd = amount
        run.usage = {"input_tokens": 2400 + index * 300, "output_tokens": 900 + index * 120}
        run.result = {"status": "needs_input" if tid == "DM-002" else "done",
                      "summary": "Export scope needs a product decision." if tid == "DM-002"
                      else "Notebook storage implemented and checked. Sample run for the README."}
        run.save()
        events.emit("run_finished", tid, at=run.finished_at, run_id=run.run_id,
                    mode=mode, harness=run.harness, model=run.model,
                    cost_usd=amount, usage=run.usage)
    return Store(garden)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="garden-readme-") as scratch:
        store = example(Path(scratch))
        uvicorn.run(create_app(store, watch=False, github=MemoryGitHub(), port=8876),
                    host="127.0.0.1", port=8876, log_level="warning")


if __name__ == "__main__":
    main()
