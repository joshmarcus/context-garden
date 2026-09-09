"""Serve a disposable garden showing a completed bounded-reclaim diagnostic."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import uvicorn
import yaml

from garden.store import Store
from garden.web.app import create_app

root = Path(tempfile.mkdtemp(prefix="cg385-ui-"))
(root / "principles").mkdir()
(root / "principles" / "00-index.md").write_text("# Principles\n")
(root / "demo" / "phase-01" / "tasks").mkdir(parents=True)
(root / "demo" / "product.md").write_text("# Demo\n")
(root / "demo" / "phase-01" / "goals.md").write_text("# Reliable admission\n")
(root / "garden.yaml").write_text(yaml.safe_dump({"name": "CG-385 capture", "products": {"demo": {}}}))
(root / ".garden").mkdir()
now = time.time()
token = "capture"
(root / ".garden" / "resource-reclaim.json").write_text(json.dumps({
    "running": False, "token": token, "finished_at": now,
    "result": {"token": token, "status": "complete", "finished_at": now,
               "headroom_before_bytes": 980 * 1024 * 1024,
               "headroom_after_bytes": 1660 * 1024 * 1024},
}))

uvicorn.run(create_app(Store(root), watch=False, host="127.0.0.1", port=8875),
            host="127.0.0.1", port=8875, log_level="error")
