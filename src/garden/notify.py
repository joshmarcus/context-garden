"""Notification hook for human-needed task transitions.

When a task needs a human (status transitions to awaiting_triage, waiting_human, failed, or
changes_requested, or when needs_human/stall/budget events occur), run a configured command
with the task details in environment variables.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def should_notify(status: str | None, needs_human: bool = False) -> bool:
    """Return True if this status change should trigger a notification."""
    if status in ("awaiting_triage", "waiting_human", "failed"):
        return True
    if status == "changes_requested" and needs_human:
        return True
    return False


def notify(
    cfg: dict[str, Any],
    task_id: str,
    status: str,
    message: str,
    pr_url: str = "",
) -> None:
    """Run the notify.command with task details in environment variables.

    Never raises; swallows all errors to avoid blocking the scheduler.
    """
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    if not cmd_config or not cmd_config.get("command"):
        return

    command = cmd_config.get("command")
    timeout = float(cmd_config.get("timeout_seconds", 30))

    env = os.environ.copy()
    env["GARDEN_TASK_ID"] = task_id
    env["GARDEN_STATUS"] = status
    env["GARDEN_MESSAGE"] = message
    env["GARDEN_PR"] = pr_url

    try:
        subprocess.run(
            command,
            shell=True,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except Exception:  # noqa: BLE001
        # Never let notification failures block the scheduler
        pass
