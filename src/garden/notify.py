"""Notification hook for human-needed task transitions.

When a task needs a human (status transitions to awaiting_triage, waiting_human, failed, or
changes_requested, or when needs_human/stall/budget events occur), run a configured command
with the task details in environment variables. See `notify.command` in `garden.yaml` and
"Configuration and environments" in docs/architecture.md.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

LOGGER = logging.getLogger("garden.notify")


def should_notify(status: str | None, needs_human: bool = False) -> bool:
    """Return True if this status change should trigger a notification."""
    if status in ("awaiting_triage", "waiting_human", "failed"):
        return True
    if status == "changes_requested" and needs_human:
        return True
    return False


def _run_command(command: str, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Run `command` and report what happened. Never raises."""
    try:
        result = subprocess.run(command, shell=True, env=env, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except Exception as e:  # noqa: BLE001 — a broken command must not crash the scheduler
        return False, f"could not run: {e}"
    if result.returncode != 0:
        detail = next((line for line in reversed((result.stderr or result.stdout or "").splitlines()) if line.strip()), "")
        return False, f"exited {result.returncode}" + (f": {detail.strip()}" if detail else "")
    return True, ""


def notify(
    cfg: dict[str, Any],
    task_id: str,
    status: str,
    message: str,
    pr_url: str = "",
) -> None:
    """Run the notify.command with task details in environment variables.

    Never raises — a broken notify.command must not block the scheduler — but unlike
    swallowing every failure outright, a command that exits non-zero, times out, or cannot
    even start is logged loudly (`garden.notify`, visible on stderr by default) so a
    misconfigured command is noticed instead of silently doing nothing.
    """
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    command = cmd_config.get("command")
    if not command:
        return

    timeout = float(cmd_config.get("timeout_seconds", 30))

    env = os.environ.copy()
    env["GARDEN_TASK_ID"] = task_id
    env["GARDEN_STATUS"] = status
    env["GARDEN_MESSAGE"] = message
    env["GARDEN_PR"] = pr_url

    ok, detail = _run_command(command, env, timeout)
    if not ok:
        LOGGER.warning("notify.command failed for %s (status=%s): %s", task_id, status, detail)


def notify_test(cfg: dict[str, Any]) -> tuple[bool, str] | None:
    """Run notify.command with a synthetic payload, for `garden doctor`.

    Returns None when notify is not configured, else (ok, detail) — detail is empty on
    success and the failure reason otherwise. Used to catch a broken command (typo, missing
    binary, unreachable webhook) before a human needs the real notification.
    """
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    command = cmd_config.get("command")
    if not command:
        return None

    timeout = float(cmd_config.get("timeout_seconds", 30))
    env = os.environ.copy()
    env["GARDEN_TASK_ID"] = "DOCTOR-TEST"
    env["GARDEN_STATUS"] = "doctor_test"
    env["GARDEN_MESSAGE"] = "garden doctor: test notification"
    env["GARDEN_PR"] = ""
    return _run_command(command, env, timeout)
