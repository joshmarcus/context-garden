"""Serve the disposable CG-337 decision-card fixture on port 8770."""

from pathlib import Path
import json

import uvicorn
import yaml

from garden.store import Store
from garden.web.app import create_app


root = Path(".pytest_cache/cg337-cards").resolve()
root.mkdir(parents=True, exist_ok=True)
(root / "garden.yaml").write_text(yaml.safe_dump({
    "name": "Decision card demonstration",
    "products": {"demo": {"repo": str(root), "base_branch": "main"}},
}))
(root / "principles").mkdir(exist_ok=True)
(root / "principles/00-index.md").write_text("# Principles\n")
(root / "demo/phase-05/tasks").mkdir(parents=True, exist_ok=True)
(root / "demo/product.md").write_text("# Demo\n")
(root / "demo/phase-05/goals.md").write_text("# Stabilization\n")


def task(task_id: str, title: str) -> None:
    data = {"id": task_id, "title": title, "status": "waiting_human", "depends_on": [],
            "priority": 1, "created": "2026-09-06T00:00:00+00:00", "updated": "2026-09-06T00:00:00+00:00"}
    path = root / f"demo/phase-05/tasks/{task_id}.md"
    path.write_text("---\n" + yaml.safe_dump(data) + "---\n\n## Goal\n\nDemonstrate an operator decision.\n")


task("DM-001", "Replace the legacy API")
task("DM-002", "Remove duplicate onboarding work")
task("DM-003", "Recover verification state")
state = {
    "DM-001": {"decision": {"kind": "no_change", "reason": "The legacy API must remain compatible.",
                              "final": "Compatibility evidence shows callers still use this API."}},
    "DM-002": {"decision": {"kind": "wont_do", "reason": "The onboarding flow already provides this behavior.",
                              "final": "Keeping both implementations would create two sources of truth."}},
    "DM-003": {"check_run": {"run_id": "missing-check", "stage": "pre_pr", "cont": {"task_status": "running"}}},
}
garden_dir = root / ".garden"
garden_dir.mkdir(exist_ok=True)
(garden_dir / "state.json").write_text(json.dumps(state))
store = Store(root)
uvicorn.run(create_app(store, watch=False, host="127.0.0.1", port=8770), host="0.0.0.0", port=8770, log_level="warning")
