"""Setup command that survives a disposable server crash and records one completion."""

from __future__ import annotations

import sys
import time
from pathlib import Path

gate = Path(sys.argv[1])
(gate / "setup-entered").touch()
while not (gate / "setup-release").exists():
    time.sleep(0.01)
with (gate / "setup-count").open("a") as count:
    count.write("completed\n")
