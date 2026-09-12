"""Drive the configured worker pool from the controller's recurring pass.

The fleet controller owns the reconciliation policy; this mixin only calls it from the two
places the controller already holds its tick lock — startup reaping and each ordinary pass —
so a single step runs at a time and no duplicate provision request can be issued.

Static SSH hosts are driven from the same two places, but never inside the pass: the tick
starts a separate probe process and returns immediately, so an unreachable host costs the
scheduler nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..fleet import FleetController
from ..ssh_probe import SSHProbeStore, probes_due


class FleetMixin:
    """One bounded reconciliation step per tick for a garden with `workers.pool`."""

    def converge_fleet(self, rep) -> None:
        """Resume the admitted pool toward its configured healthy count.

        Nothing happens in a garden with no `workers.pool` block, and the controller's own
        cadence, backoff and breaker decide whether this pass acts at all.
        """
        controller = FleetController(self.cfg)
        if controller.settings is None:
            return
        before = controller.state.read()
        record = controller.converge()
        detail = str(record.get("last_outcome") or "")
        # This pass runs every tick, so only a changed reading earns a line in the log.
        if detail and detail != str(before.get("last_outcome") or ""):
            self.log(f"fleet: {detail}")
        action = str(record.get("action_required") or "")
        if action:
            rep.errors.append(f"fleet: {action}")

    def probe_ssh_workers(self) -> None:
        """Start one bounded probe pass for the configured static hosts and wait for none of it.

        A garden with no `ssh.hosts` entry, and one probed inside its configured cadence, start
        nothing.  Otherwise the pass runs in a detached process of its own: whatever the hosts
        do, this tick pays for one `Popen` and the probe's own timeout binds the child.
        """
        if not probes_due(self.cfg):
            return
        # Claim the cadence before the child exists.  A pass that runs long is then not started
        # again on the next tick, and one that dies before it writes leaves a claim that
        # expires on its own rather than a lock nobody holds.
        SSHProbeStore(Path(self.cfg.garden_dir)).request()
        # Import from the loaded package, never from a task worktree that may shadow it.
        root = Path(__file__).resolve().parents[2]
        subprocess.Popen(
            [sys.executable, "-m", "garden.ssh_probe", str(self.cfg.root)],
            cwd=str(root), env={**os.environ, "PYTHONPATH": str(root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
